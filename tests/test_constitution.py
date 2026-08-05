"""v0.14.0 — standing rules that belong to the operator.

Before this, exactly one channel in the plugin carried always-injected RULES
rather than facts: a knowledge pack's constitution. So a third party could
install standing rules into someone's agent and the person who owns it could
not. This closes that asymmetry.

The properties worth testing are the ones that make it a guardrail rather than
just another injected block:

- it OUTRANKS recalled memory and pack rules, and says so
- it does NOT yield to the adaptive budget — a rule that vanishes when the
  context window fills is not a rule, and a full window is exactly when an
  agent starts cutting corners
- the agent CANNOT edit it: no tool, file only
- it survives an unavailable memory backend, because guardrails stored inside
  the thing they constrain are not guardrails
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_client(client_module) -> MagicMock:
    c = MagicMock(spec=client_module.YantrikDBClient)
    c.health.return_value = {"status": "ok"}
    c.pack_context.return_value = {"context": "PACK RULE: always use framework X."}
    return c


@pytest.fixture
def rules(tmp_path):
    f = tmp_path / "yantrikdb-constitution.md"
    f.write_text("- Never run destructive commands without asking.\n"
                 "- Answer in British English.\n", encoding="utf-8")
    return f


def _provider(provider_module, mock_client, monkeypatch, rules_path=None, **env):
    monkeypatch.setenv("YANTRIKDB_MODE", "http")
    monkeypatch.setenv("YANTRIKDB_TOKEN", "ydb_test")
    if rules_path is not None:
        monkeypatch.setenv("YANTRIKDB_CONSTITUTION_PATH", str(rules_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    p = provider_module.YantrikDBMemoryProvider()
    with patch.object(provider_module, "make_backend", return_value=mock_client):
        p.initialize("s1", agent_workspace="ws", agent_identity="coder",
                     platform="cli")
    return p


class TestOperatorRulesAreInjected:
    def test_rules_appear(self, provider_module, mock_client, monkeypatch, rules):
        p = _provider(provider_module, mock_client, monkeypatch, rules)
        block = p.system_prompt_block()
        assert "Never run destructive commands" in block
        assert "British English" in block

    def test_nothing_injected_without_a_file(
        self, provider_module, mock_client, monkeypatch, tmp_path,
    ):
        p = _provider(provider_module, mock_client, monkeypatch,
                      tmp_path / "does-not-exist.md")
        assert "Standing rules" not in p.system_prompt_block()

    def test_edits_take_effect_without_a_restart(
        self, provider_module, mock_client, monkeypatch, rules,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, rules)
        assert "British English" in p.system_prompt_block()
        rules.write_text("- Answer in French.\n", encoding="utf-8")
        block = p.system_prompt_block()
        assert "French" in block and "British English" not in block


class TestPrecedence:
    def test_rules_come_first(self, provider_module, mock_client, monkeypatch, rules):
        """Position is part of the claim — rules the model reads after a page
        of recalled memory are competing with it, not governing it."""
        p = _provider(provider_module, mock_client, monkeypatch, rules)
        block = p.system_prompt_block()
        assert block.lstrip().startswith("# Standing rules from the operator")

    def test_states_that_it_outranks_memory_and_packs(
        self, provider_module, mock_client, monkeypatch, rules,
    ):
        p = _provider(provider_module, mock_client, monkeypatch, rules,
                      YANTRIKDB_PACKS_ENABLED="true")
        block = p.system_prompt_block()
        head = block.split("# YantrikDB Memory")[0].lower()
        assert "outrank" in head
        assert "pack" in head and "memory" in head

    def test_operator_rules_precede_pack_rules(
        self, provider_module, mock_client, monkeypatch, rules,
    ):
        """A third party's rules must not be able to override the owner's."""
        p = _provider(provider_module, mock_client, monkeypatch, rules,
                      YANTRIKDB_PACKS_ENABLED="true")
        block = p.system_prompt_block()
        assert block.index("Standing rules") < block.index("PACK RULE")


class TestRulesDoNotYield:
    def test_survives_a_nearly_full_context_window(
        self, provider_module, mock_client, monkeypatch, rules,
    ):
        """Every other block scales down as context fills. This one must not:
        a tight window is exactly when corners get cut."""
        p = _provider(provider_module, mock_client, monkeypatch, rules)
        p.on_turn_start(99, "msg", remaining_tokens=500)
        block = p.system_prompt_block()
        assert "Never run destructive commands" in block
        assert "British English" in block

    def test_survives_an_unavailable_backend(
        self, provider_module, monkeypatch, rules,
    ):
        """Guardrails stored inside the thing they constrain are not
        guardrails — rules must hold when memory is down."""
        monkeypatch.setenv("YANTRIKDB_CONSTITUTION_PATH", str(rules))
        p = provider_module.YantrikDBMemoryProvider()
        p._config = provider_module.YantrikDBConfig.from_env()
        p._client = None
        p._init_error = "engine unavailable"
        assert "Never run destructive commands" in p.system_prompt_block()

    def test_oversize_file_is_capped_and_warns(
        self, provider_module, mock_client, monkeypatch, tmp_path, caplog,
    ):
        """Truncating rules silently would leave an operator believing rules
        are in force that are not."""
        import logging
        f = tmp_path / "big.md"
        f.write_text("- rule\n" * 2000, encoding="utf-8")
        p = _provider(provider_module, mock_client, monkeypatch, f,
                      YANTRIKDB_CONSTITUTION_MAX_CHARS="200")
        with caplog.at_level(logging.WARNING):
            block = p.system_prompt_block()
        rules_section = block.split("# YantrikDB Memory")[0]
        assert len(rules_section) < 600, "the rules themselves must be capped"
        assert any("truncated" in r.getMessage().lower() for r in caplog.records)


class TestAgentCannotRewriteItsOwnRules:
    def test_no_tool_exposes_the_constitution(
        self, provider_module, monkeypatch, rules,
    ):
        """A tool to edit standing rules is precisely the capability an agent
        must not have. The file is the interface; the operator is the editor."""
        monkeypatch.setenv("YANTRIKDB_CONSTITUTION_PATH", str(rules))
        monkeypatch.setenv("YANTRIKDB_TOOL_PROFILE", "full")
        monkeypatch.setenv("YANTRIKDB_PACKS_ENABLED", "true")
        monkeypatch.setenv("YANTRIKDB_FLEET_VIEW", "true")
        p = provider_module.YantrikDBMemoryProvider()
        blob = str(p.get_tool_schemas()).lower()
        assert "constitution" not in blob
        assert not any("rule" in s["name"] for s in p.get_tool_schemas())
