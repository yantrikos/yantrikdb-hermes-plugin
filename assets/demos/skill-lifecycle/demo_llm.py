#!/usr/bin/env python3
"""LLM-driven skill-lifecycle demo.

A real LLM (OpenAI's gpt-4o-mini via the chat-completions API) receives
the plugin's 11 tool schemas and decides when to call each one. The
plugin's ``handle_tool_call`` dispatch path is the same entry point
Hermes invokes when its agent loop encounters a yantrikdb tool — only
the LLM call and tool-dispatch are surfaced here; the full agent
orchestration is what Hermes adds on top.

This demo runs at "readable" pacing — explicit pauses between beats
so viewers can follow each step rather than blinking and missing it.
End-to-end ~60 seconds. Two sessions, two skills, real autonomy in both.

Requires:
    pip install openai yantrikdb yantrikdb-hermes-plugin
    OPENAI_API_KEY in env
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from textwrap import indent, wrap

# Ephemeral demo home — substrate is wiped between runs.
DEMO_HOME = Path(tempfile.mkdtemp(prefix="yantrikdb_hermes_llm_demo_"))
os.environ["YANTRIKDB_DB_PATH"] = str(DEMO_HOME / "memory.db")
os.environ["YANTRIKDB_MODE"] = "embedded"
os.environ["YANTRIKDB_NAMESPACE"] = "demo"
os.environ["YANTRIKDB_SKILLS_ENABLED"] = "true"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

MODEL = os.environ.get("DEMO_LLM_MODEL", "gpt-4o-mini")
MAX_TOOL_ITERATIONS = 6

# Pacing — tuned so the recording reads at human-speed without dragging.
BEAT_SHORT = 1.0
BEAT_MED = 1.8
BEAT_LONG = 2.6


def banner(text: str) -> None:
    line = "─" * max(64, len(text) + 6)
    print(f"\n{line}\n  {text}\n{line}")
    sys.stdout.flush()


def narrate(text: str) -> None:
    """Narrative comment between tool calls so viewers follow the story."""
    for line in wrap(text, width=78):
        print(f"  · {line}")
    sys.stdout.flush()


def pretty_args(args: dict, max_val: int = 120) -> str:
    out_parts = []
    for k, v in args.items():
        s = json.dumps(v) if not isinstance(v, str) else json.dumps(v)
        if len(s) > max_val:
            s = s[: max_val - 3] + "..."
        out_parts.append(f"{k}={s}")
    return ", ".join(out_parts)


def pretty_result(result_json: str) -> str:
    """Format tool result for display — readable, not truncated to oblivion."""
    try:
        obj = json.loads(result_json)
    except Exception:
        return result_json[:300]
    return json.dumps(obj, indent=2)[:600]


def pause(s: float = BEAT_MED) -> None:
    sys.stdout.flush()
    time.sleep(s)


def to_openai_tools(plugin_schemas: list[dict]) -> list[dict]:
    """Plugin returns OpenAI-tool-compatible schemas already."""
    return [{"type": "function", "function": s} for s in plugin_schemas]


def run_agent_turn(client, provider, system: str, user: str) -> None:
    tools = to_openai_tools(provider.get_tool_schemas())

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    print()
    narrate(f"USER → agent:")
    for line in wrap(user, width=76):
        print(f"      {line}")
    pause(BEAT_MED)

    for step in range(MAX_TOOL_ITERATIONS):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.1,
        )
        msg = resp.choices[0].message
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in (msg.tool_calls or [])
            ] or None,
        })

        if not msg.tool_calls:
            if msg.content:
                print()
                narrate(f"{MODEL} replies:")
                for line in wrap(msg.content, width=76):
                    print(f"      {line}")
                pause(BEAT_MED)
            return

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            print()
            print(f"  ⚙  {MODEL} → {name}(")
            for line in pretty_args(args, max_val=120).split(", "):
                print(f"        {line}")
            print(f"     )")
            pause(BEAT_SHORT)
            result = provider.handle_tool_call(name, args)
            print(f"  ← plugin returned:")
            print(indent(pretty_result(result), "        "))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            pause(BEAT_LONG)


def show_substrate_stats(provider, label: str, namespace: str = "skill_substrate") -> None:
    raw = provider.handle_tool_call("yantrikdb_stats", {"namespace": namespace})
    try:
        d = json.loads(raw)
    except Exception:
        return
    print(f"  ▸ substrate / {namespace} ({label}):")
    print(f"        active_memories={d.get('active_memories', 0)}, "
          f"operations={d.get('operations', 0)}, "
          f"open_conflicts={d.get('open_conflicts', 0)}")
    sys.stdout.flush()


def main() -> None:
    try:
        from openai import OpenAI
    except ImportError:
        print("  ! openai SDK not installed. pip install openai", file=sys.stderr)
        sys.exit(2)
    if not os.environ.get("OPENAI_API_KEY"):
        print("  ! OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(2)

    from yantrikdb_hermes_plugin import YantrikDBMemoryProvider

    banner("LLM-driven skill-lifecycle demo")
    print(f"  model:      {MODEL}")
    print(f"  substrate:  {DEMO_HOME / 'memory.db'}  (ephemeral, wiped between runs)")
    print(f"  tools:      {len(YantrikDBMemoryProvider().get_tool_schemas())} (the plugin's full surface)")
    pause(BEAT_LONG)

    client = OpenAI()

    # ─────────────────────────────────────────────────────────────────
    # SESSION 1 — observe a pattern, crystallize a skill
    # ─────────────────────────────────────────────────────────────────
    banner("Session 1 — agent observes a useful pattern")
    provider1 = YantrikDBMemoryProvider()
    provider1.initialize("demo-llm-s1", hermes_home=str(DEMO_HOME))
    show_substrate_stats(provider1, "before")
    pause(BEAT_MED)

    narrate(
        "The user just shipped their third clean release using the same procedure. "
        "They tell the agent. The agent decides whether the pattern is worth "
        "crystallizing as a reusable skill."
    )
    pause(BEAT_LONG)

    system_1 = (
        "You are a Hermes Agent with yantrikdb tools for persistent memory and skills. "
        "When a user describes a workflow they've used successfully multiple times, "
        "crystallize it via yantrikdb_skill_define so future sessions can find it. "
        "Use a clear dot-separated skill_id. After defining, briefly confirm. Stop there."
    )
    user_1 = (
        "I just shipped my third clean yantrikos release this week using the same "
        "procedure: feature branch with PR, CI green on Python 3.11-3.14, "
        "squash-merge to main with CHANGELOG entry, tag vX.Y.Z and push, "
        "gh release create (which fires the Publish workflow), verify on PyPI. "
        "Crystallize this so future-me finds it."
    )
    run_agent_turn(client, provider1, system_1, user_1)

    pause(BEAT_MED)
    show_substrate_stats(provider1, "after define")
    pause(BEAT_LONG)
    provider1.shutdown()

    banner("[ session 1 ended — agent state torn down — substrate persists ]")
    pause(BEAT_LONG)

    # ─────────────────────────────────────────────────────────────────
    # SESSION 2 — fresh agent searches before acting
    # ─────────────────────────────────────────────────────────────────
    banner("Session 2 — different agent instance, same substrate, new task")
    provider2 = YantrikDBMemoryProvider()
    provider2.initialize("demo-llm-s2", hermes_home=str(DEMO_HOME))
    show_substrate_stats(provider2, "session 2 begins")
    pause(BEAT_MED)

    narrate(
        "Fresh provider, zero in-memory context from session 1. But the substrate "
        "still holds the skill. A well-trained agent searches before acting — "
        "let's see if the model decides to."
    )
    pause(BEAT_LONG)

    system_2 = (
        "You are a Hermes Agent. Before performing any release-related task, use "
        "yantrikdb_skill_search to look up relevant procedures from past sessions. "
        "If you find one, follow it and then record the outcome via "
        "yantrikdb_skill_outcome. Be concise."
    )
    user_2 = (
        "I need to ship v0.4.13 of yantrikdb-hermes-plugin to PyPI. Search for any "
        "relevant skill from previous sessions, follow it, and record the outcome "
        "(pretend it succeeded — no need to actually run git)."
    )
    run_agent_turn(client, provider2, system_2, user_2)

    pause(BEAT_MED)
    show_substrate_stats(provider2, "after search + use + outcome")
    pause(BEAT_LONG)
    provider2.shutdown()

    banner("Autonomy loop closed — same model, two sessions, real persistence")
    print(f"  ▸ model:     {MODEL} — picked the skill_id, applies_to, body, search")
    print(f"               query, and outcome note autonomously from the prompts")
    print(f"  ▸ substrate: 1 skill, outcome ledger appended, ready for session 3")
    print(f"               (which would see this skill ranked higher next time)")
    print(f"  ▸ docs:      https://yantrikdb.com/guides/autonomous-skills/")
    pause(BEAT_LONG)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n  ! demo failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
