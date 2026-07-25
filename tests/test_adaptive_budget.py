"""v0.10.0 — adaptive prompt budget driven by `on_turn_start`.

Hermes passes `remaining_tokens` on every turn. Everything the plugin injects
into the system prompt competes with the conversation for one window, and it is
most expensive exactly when that window is nearly full. These tests pin the
taper, and — more importantly — pin the two ways it must never misbehave:
silently shrinking when the host said nothing, and vanishing a block entirely
while budget remains.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def provider(provider_module, monkeypatch):
    p = provider_module.YantrikDBMemoryProvider()
    p._config = provider_module.YantrikDBConfig.from_env()
    return p


class TestBudgetScale:
    def test_unknown_remaining_behaves_exactly_as_before(self, provider):
        """The load-bearing default: absent information, change nothing.

        An older Hermes never calls on_turn_start, and a caller may omit the
        kwarg. Guessing 'probably tight' there would silently degrade memory
        for hosts that were working fine.
        """
        assert provider._remaining_tokens is None
        assert provider._budget_scale() == 1.0
        assert provider._budgeted(5) == 5

    def test_plenty_of_room_is_full_budget(self, provider):
        provider.on_turn_start(1, "hi", remaining_tokens=120_000)
        assert provider._budget_scale() == 1.0
        assert provider._budgeted(5) == 5

    def test_below_low_watermark_yields_nothing(self, provider):
        provider.on_turn_start(9, "hi", remaining_tokens=2_000)
        assert provider._budget_scale() == 0.0
        assert provider._budgeted(5) == 0

    def test_between_watermarks_tapers(self, provider):
        cfg = provider._config
        mid = (cfg.prompt_budget_low_watermark + cfg.prompt_budget_high_watermark) // 2
        provider.on_turn_start(4, "hi", remaining_tokens=mid)
        scale = provider._budget_scale()
        assert 0.0 < scale < 1.0
        assert 0 < provider._budgeted(6) < 6

    def test_never_rounds_a_live_budget_down_to_zero(self, provider):
        """While any budget remains, a block degrades to its single most
        relevant entry rather than disappearing without explanation."""
        cfg = provider._config
        just_above = cfg.prompt_budget_low_watermark + 1
        provider.on_turn_start(4, "hi", remaining_tokens=just_above)
        assert provider._budget_scale() > 0
        assert provider._budgeted(5) >= 1

    def test_opt_out_disables_scaling(self, provider_module, monkeypatch):
        monkeypatch.setenv("YANTRIKDB_ADAPTIVE_PROMPT_BUDGET", "false")
        p = provider_module.YantrikDBMemoryProvider()
        p._config = provider_module.YantrikDBConfig.from_env()
        p.on_turn_start(1, "hi", remaining_tokens=500)
        assert p._budget_scale() == 1.0
        assert p._budgeted(5) == 5


class TestHookContract:
    def test_accepts_hermes_call_shape_and_unknown_kwargs(self, provider):
        """Hermes documents that kwargs grow over time; absorbing them is what
        stops a future release from silently killing this hook."""
        provider.on_turn_start(
            3, "a message", remaining_tokens=50_000, model="qwen",
            platform="cli", tool_count=7, some_future_kwarg="x",
        )
        assert provider._turn_number == 3
        assert provider._remaining_tokens == 50_000

    def test_non_numeric_remaining_is_ignored_not_crashed(self, provider):
        provider.on_turn_start(1, "hi", remaining_tokens=None)
        assert provider._remaining_tokens is None
        provider.on_turn_start(2, "hi", remaining_tokens="lots")
        assert provider._remaining_tokens is None
        assert provider._budget_scale() == 1.0

    def test_does_no_backend_work(self, provider_module):
        """This runs on every turn; a provider that spends the turn's first
        milliseconds on maintenance is one people disable."""
        p = provider_module.YantrikDBMemoryProvider()
        p._client = None          # any backend call would raise or no-op loudly
        p._config = provider_module.YantrikDBConfig.from_env()
        p.on_turn_start(1, "hi", remaining_tokens=1_000)
        assert p._dropped_writes == 0
