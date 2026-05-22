"""Live embedded end-to-end test for the trigger consumer tools (v0.4.13).

Walks the producer → consumer loop with a real yantrikdb engine in a
temp dir: write memories, run think(), list pending triggers, close them
out via acknowledge / dismiss / act_on, verify the count actually drops.

This is the regression that closes #17 — without these tools, triggers
the engine produces would accumulate forever because the plugin only
exposed the producer side.

Skipped automatically if yantrikdb engine isn't importable (CI matrix
that doesn't install the heavy engine extras still runs the unit tests).
"""
from __future__ import annotations

import json

import pytest

# Skip the whole module if yantrikdb engine isn't available.
yantrikdb_engine = pytest.importorskip("yantrikdb")
# Skip if running inside the plugin source tree where `yantrikdb`
# resolves to the plugin package instead of the engine. Detect by
# checking for the `YantrikDB` engine class.
if not hasattr(yantrikdb_engine, "YantrikDB"):
    pytest.skip(
        "yantrikdb engine not on sys.path (shadowed by plugin package); "
        "run from outside the plugin source tree to exercise this test",
        allow_module_level=True,
    )


@pytest.fixture
def embedded_provider(provider_module, monkeypatch, tmp_path):
    """Spin up a real-engine provider against a temp SQLite DB."""
    db_path = tmp_path / "trigger-e2e.db"
    monkeypatch.setenv("YANTRIKDB_MODE", "embedded")
    monkeypatch.setenv("YANTRIKDB_DB_PATH", str(db_path))
    monkeypatch.delenv("YANTRIKDB_TOKEN", raising=False)
    # Use a deterministic small embedder; potion-base-8M is the smallest
    # the engine currently ships in builtin names.
    monkeypatch.setenv("YANTRIKDB_EMBEDDER", "potion-base-8M")
    monkeypatch.setenv("YANTRIKDB_EMBEDDING_DIM", "256")
    p = provider_module.YantrikDBMemoryProvider()
    p.initialize(
        "sess-trigger-e2e",
        agent_workspace="workspace",
        agent_identity="coder",
        platform="cli",
    )
    yield p
    # No explicit teardown — provider holds the engine handle; Python GC
    # closes it; tmp_path is cleaned by pytest.


class TestTriggerLifecycle:
    def test_pending_triggers_returns_list_on_fresh_db(self, embedded_provider):
        """Fresh DB → call succeeds, returns a structured list (likely empty)."""
        out = embedded_provider.handle_tool_call("yantrikdb_pending_triggers", {})
        parsed = json.loads(out)
        assert "count" in parsed
        assert "triggers" in parsed
        assert isinstance(parsed["triggers"], list)
        assert parsed["count"] == len(parsed["triggers"])

    def test_full_lifecycle_against_real_engine(self, embedded_provider):
        """Plant memories, run think(), exercise the consumer loop.

        The exact number of triggers the engine produces from a small
        seed is implementation-dependent — what we're verifying here is:
        (a) the four new tools dispatch through to the real engine
            without raising,
        (b) acknowledge / dismiss / act_on each reduce pending_triggers
            by 1 when there's something to consume,
        (c) the wire shapes match what the agent will see.
        """
        # Seed a couple of conflicting facts so think() has material.
        for text, importance, domain in [
            ("The deploy threshold is 0.9", 0.9, "deploy"),
            ("Actually the deploy threshold is 0.95", 0.9, "deploy"),
            ("The deploy threshold dropped to 0.85", 0.9, "deploy"),
        ]:
            r = embedded_provider.handle_tool_call(
                "yantrikdb_remember",
                {"text": text, "importance": importance, "domain": domain},
            )
            parsed = json.loads(r)
            assert parsed.get("stored") is True, f"remember failed: {parsed}"

        # Run think — this is the side that produces triggers.
        think_out = embedded_provider.handle_tool_call(
            "yantrikdb_think", {"run_pattern_mining": True},
        )
        think_parsed = json.loads(think_out)
        assert "triggers" in think_parsed  # shape contract

        # List pending. We don't assert > 0 because trigger emission
        # depends on engine policy that may have moved; the test asserts
        # the call returns a valid shape regardless.
        pending_out = embedded_provider.handle_tool_call(
            "yantrikdb_pending_triggers", {"limit": 50},
        )
        pending = json.loads(pending_out)
        initial_count = pending["count"]
        assert isinstance(pending["triggers"], list)

        if initial_count == 0:
            pytest.skip(
                "engine produced 0 triggers on this small seed — "
                "consumer-tool wire shapes verified, but no lifecycle "
                "transitions to exercise. The dispatch-level tests in "
                "test_provider.py cover transition correctness with mocks."
            )

        # If we got triggers: walk acknowledge → dismiss → act_on,
        # verifying each closes one trigger.
        ops = [
            ("yantrikdb_acknowledge_trigger", "acknowledged"),
            ("yantrikdb_dismiss_trigger", "dismissed"),
            ("yantrikdb_act_on_trigger", "acted"),
        ]
        for op_name, ok_key in ops:
            current = json.loads(embedded_provider.handle_tool_call(
                "yantrikdb_pending_triggers", {"limit": 50},
            ))
            if current["count"] == 0:
                break
            trigger_id = (
                current["triggers"][0].get("trigger_id")
                or current["triggers"][0].get("id")
            )
            assert trigger_id, f"trigger has no id field: {current['triggers'][0]}"
            out = embedded_provider.handle_tool_call(
                op_name, {"trigger_id": trigger_id},
            )
            parsed = json.loads(out)
            assert parsed["trigger_id"] == trigger_id
            assert parsed.get(ok_key) is True, f"{op_name} did not return {ok_key}=True: {parsed}"

            # Verify pending count strictly decreased.
            after = json.loads(embedded_provider.handle_tool_call(
                "yantrikdb_pending_triggers", {"limit": 50},
            ))
            assert after["count"] < current["count"], (
                f"{op_name} did not close trigger {trigger_id}: "
                f"count was {current['count']}, now {after['count']}"
            )
