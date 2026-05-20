#!/usr/bin/env python3
"""LLM-driven version of the skill-lifecycle demo.

Unlike demo.py — where the "agent" is scripted and only the tool
calls run live — this script lets a real LLM (OpenAI's gpt-4o-mini
via the standard chat-completions API) decide when to invoke the
plugin's tools. The plugin's tool schemas are surfaced via
``YantrikDBMemoryProvider.get_tool_schemas()`` and registered with
the chat completion call. When the model emits a tool call, we
dispatch it via ``provider.handle_tool_call`` (the same entry point
Hermes uses) and feed the result back into the conversation.

This is the same architecture Hermes wraps in its full agent loop —
shown here as a focused script so the LLM-driven flow is auditable.

Requires:
    pip install openai yantrikdb yantrikdb-hermes-plugin
    OPENAI_API_KEY in env (or wherever your provider routes — model
    name + base_url are configurable below).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

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


def banner(text: str) -> None:
    line = "─" * max(60, len(text) + 4)
    print(f"\n{line}\n  {text}\n{line}")


def short(s: str, n: int = 180) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + " …"


def to_openai_tools(plugin_schemas: list[dict]) -> list[dict]:
    """Plugin returns OpenAI-tool-compatible schemas already. Just
    wrap each in the {type: 'function', function: {...}} envelope
    the chat-completions API expects."""
    out = []
    for s in plugin_schemas:
        out.append({"type": "function", "function": s})
    return out


def run_agent_loop(client, provider, system: str, user: str, *, max_iter: int = MAX_TOOL_ITERATIONS):
    tools = to_openai_tools(provider.get_tool_schemas())

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    print(f"\n  > user: {short(user, 220)}")

    for step in range(max_iter):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.1,
        )
        msg = resp.choices[0].message
        messages.append({"role": "assistant",
                         "content": msg.content,
                         "tool_calls": [
                             {"id": tc.id, "type": "function",
                              "function": {"name": tc.function.name,
                                           "arguments": tc.function.arguments}}
                             for tc in (msg.tool_calls or [])
                         ] or None})

        if not msg.tool_calls:
            print(f"\n  < {MODEL}: {short(msg.content or '', 280)}")
            return msg.content

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            print(f"\n  ⚙  {MODEL} → {name}({short(json.dumps(args), 160)})")
            result = provider.handle_tool_call(name, args)
            print(f"  ← plugin: {short(result, 200)}")
            messages.append({"role": "tool",
                             "tool_call_id": tc.id,
                             "content": result})

    print(f"\n  ! max tool iterations ({max_iter}) reached")
    return None


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
    print(f"  Model: {MODEL}")
    print(f"  Substrate: {DEMO_HOME / 'memory.db'}")
    print(f"  Tools exposed by plugin: {len(YantrikDBMemoryProvider().get_tool_schemas())}")

    client = OpenAI()

    # ---- Session 1 ----
    banner("Session 1: agent observes a pattern and crystallizes a skill")
    provider1 = YantrikDBMemoryProvider()
    provider1.initialize("demo-llm-session-1", hermes_home=str(DEMO_HOME))

    system = (
        "You are a Hermes Agent with access to yantrikdb tools for persistent "
        "memory and skills. When the user describes a workflow they've used "
        "successfully multiple times, use yantrikdb_skill_define to crystallize "
        "it as a reusable procedure. skill_id should be dot-separated lowercase. "
        "Be concise — call the tool, briefly confirm, stop."
    )
    user1 = (
        "I just shipped my third clean yantrikos release this week using the same "
        "procedure: feature branch with PR, CI green on Python 3.11-3.14, "
        "squash-merge to main with CHANGELOG entry, tag vX.Y.Z and push, gh "
        "release create (fires Publish workflow), verify on PyPI. Crystallize "
        "this as a skill so future-me finds it."
    )
    run_agent_loop(client, provider1, system, user1)
    provider1.shutdown()

    print("\n  --- session 1 ended; substrate persists ---")
    time.sleep(0.5)

    # ---- Session 2 ----
    banner("Session 2: fresh agent searches the substrate before acting")
    provider2 = YantrikDBMemoryProvider()
    provider2.initialize("demo-llm-session-2", hermes_home=str(DEMO_HOME))

    system2 = (
        "You are a Hermes Agent. Before performing any release-related task, "
        "use yantrikdb_skill_search to look up relevant procedures from past "
        "sessions. After acting on a skill, call yantrikdb_skill_outcome to "
        "record whether it worked. Be concise."
    )
    user2 = (
        "I need to ship v0.4.13 of yantrikdb-hermes-plugin. Search for any "
        "relevant skill from previous sessions, follow it, and record the "
        "outcome (pretend it succeeded — no need to actually run git)."
    )
    run_agent_loop(client, provider2, system2, user2)
    provider2.shutdown()

    banner("Done — autonomy loop closed")
    print(f"  Substrate: {DEMO_HOME / 'memory.db'}")
    print(f"  Model: {MODEL}")
    print("  The LLM chose when to call each tool. The plugin handled dispatch.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n  ! demo failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
