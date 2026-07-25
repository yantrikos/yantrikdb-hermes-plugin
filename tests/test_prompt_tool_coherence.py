"""The system prompt must only name tools the model can actually see.

Caught on a real Hermes v0.15.1 install, not in CI: after v0.10.0 hid `think`
behind the `full` profile, the prompt still instructed the agent to "Run
`yantrikdb_think` at natural break points" — telling the model to call a tool
it had no schema for. Nothing errored; it just produced an agent being asked
to do something it couldn't correctly do.

This class of bug is invisible to unit tests that check the prompt and the
tool list separately, which is exactly why it survived to a live run. These
tests check them against each other.
"""

from __future__ import annotations

import re

import pytest

_TOOL_RE = re.compile(r"`(yantrikdb_[a-z_]+)`")


def _provider(provider_module, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    p = provider_module.YantrikDBMemoryProvider()
    p._config = provider_module.YantrikDBConfig.from_env()
    p._namespace = "hermes:test:agent"
    p._session_id = "s1"
    p._client = object()  # non-None so the block renders the active variant
    return p


@pytest.mark.parametrize("profile", ["core", "full"])
def test_prompt_never_names_an_unexposed_tool(
    provider_module, monkeypatch, profile,
):
    p = _provider(provider_module, monkeypatch, YANTRIKDB_TOOL_PROFILE=profile)
    exposed = {s["name"] for s in p.get_tool_schemas()}
    named = set(_TOOL_RE.findall(p.system_prompt_block()))
    missing = named - exposed
    assert not missing, (
        f"profile={profile}: prompt instructs the model to use {sorted(missing)}, "
        f"which it cannot see. Exposed: {sorted(exposed)}"
    )


def test_core_profile_explains_consolidation_is_automatic(
    provider_module, monkeypatch,
):
    """Dropping the instruction isn't enough — silence would read as 'this
    memory never consolidates'. Say that it happens on its own."""
    p = _provider(provider_module, monkeypatch, YANTRIKDB_TOOL_PROFILE="core")
    block = p.system_prompt_block()
    assert "yantrikdb_think" not in block
    assert "automatically" in block.lower()


def test_full_profile_still_instructs_think(provider_module, monkeypatch):
    p = _provider(provider_module, monkeypatch, YANTRIKDB_TOOL_PROFILE="full")
    assert "yantrikdb_think" in p.system_prompt_block()
