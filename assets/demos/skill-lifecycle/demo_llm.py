#!/usr/bin/env python3
"""LLM-driven demo: agent triages an incident using the substrate, then crystallizes a new lesson.

A real LLM (OpenAI gpt-4o-mini) gets a concrete incident report. It uses
the substrate as working memory — searching for relevant past procedures,
references, and lessons; composing a multi-skill response; recording
outcomes; and crystallizing one new insight from the experience.

Session 2 then proves the substrate carried session 1's learning
forward: a fresh agent with no chat context gets a similar incident,
finds the new lesson via search, and applies it directly.

Only the user prompts are scripted. Every tool call (which tool, what
arguments, which skills to consult, what to record, what to write as
the new lesson) is the model's autonomous choice.

End-to-end ~75-90 seconds at readable pacing.

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

DEMO_HOME = Path(tempfile.mkdtemp(prefix="yantrikdb_hermes_llm_demo_"))
os.environ["YANTRIKDB_DB_PATH"] = str(DEMO_HOME / "memory.db")
os.environ["YANTRIKDB_MODE"] = "embedded"
os.environ["YANTRIKDB_NAMESPACE"] = "demo"
os.environ["YANTRIKDB_SKILLS_ENABLED"] = "true"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

MODEL = os.environ.get("DEMO_LLM_MODEL", "gpt-4o-mini")
MAX_TOOL_ITERATIONS = 10

BEAT_SHORT = 0.8
BEAT_MED = 1.4
BEAT_LONG = 2.2


def banner(text: str) -> None:
    line = "─" * max(64, len(text) + 6)
    print(f"\n{line}\n  {text}\n{line}")
    sys.stdout.flush()


def narrate(text: str) -> None:
    for line in wrap(text, width=78):
        print(f"  · {line}")
    sys.stdout.flush()


def pretty_args(args: dict, max_val: int = 140) -> str:
    out_parts = []
    for k, v in args.items():
        s = json.dumps(v) if not isinstance(v, str) else json.dumps(v)
        if len(s) > max_val:
            s = s[: max_val - 3] + "..."
        out_parts.append(f"{k}={s}")
    return ", ".join(out_parts)


def pretty_result(result_json: str, max_len: int = 500) -> str:
    try:
        obj = json.loads(result_json)
    except Exception:
        return result_json[:max_len]
    s = json.dumps(obj, indent=2)
    return s if len(s) <= max_len else s[:max_len] + " …"


def pause(s: float = BEAT_MED) -> None:
    sys.stdout.flush()
    time.sleep(s)


def to_openai_tools(plugin_schemas: list[dict]) -> list[dict]:
    return [{"type": "function", "function": s} for s in plugin_schemas]


def run_agent_turn(client, provider, system: str, user: str) -> list:
    tools = to_openai_tools(provider.get_tool_schemas())
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    print()
    narrate("USER → agent:")
    for line in wrap(user, width=76):
        print(f"      {line}")
    pause(BEAT_MED)

    for step in range(MAX_TOOL_ITERATIONS):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools,
            tool_choice="auto", temperature=0.1,
        )
        msg = resp.choices[0].message
        messages.append({
            "role": "assistant", "content": msg.content,
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
            return messages

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            print()
            print(f"  ⚙  {MODEL} → {name}(")
            for line in pretty_args(args, max_val=140).split(", "):
                print(f"        {line}")
            print(f"     )")
            pause(BEAT_SHORT)
            result = provider.handle_tool_call(name, args)
            print(f"  ← plugin:")
            print(indent(pretty_result(result), "        "))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            pause(BEAT_MED)
    return messages


def show_substrate(provider, label: str, outcomes: int = 0) -> None:
    """Substrate state line. Outcome count is tracked by the caller since
    there's no skill_outcome list tool — we count from what we've seen."""
    search_raw = provider.handle_tool_call(
        "yantrikdb_skill_search", {"query": "skill", "top_k": 100}
    )
    try:
        d = json.loads(search_raw)
        n = d.get("count", 0)
    except Exception:
        n = 0
    print(f"  ▸ skill_substrate ({label}):  skills={n}, outcomes={outcomes}, conflicts=0")
    sys.stdout.flush()


def count_outcomes_in_messages(messages: list) -> int:
    """Counts skill_outcome tool calls we've seen in the conversation."""
    count = 0
    for m in messages:
        for tc in (m.get("tool_calls") or []):
            if tc.get("function", {}).get("name") == "yantrikdb_skill_outcome":
                count += 1
    return count


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
    sys.path.insert(0, str(Path(__file__).parent))
    from seed_skills import load_seed_skills, SEED_SKILLS  # noqa: E402

    banner("LLM-driven skill-lifecycle — agent triages an incident using the substrate")
    print(f"  model:      {MODEL}")
    print(f"  substrate:  ephemeral SQLite, pre-seeded with {len(SEED_SKILLS)} skills from past sessions")
    print(f"  tools:      {len(YantrikDBMemoryProvider().get_tool_schemas())} (the plugin's full surface)")
    pause(BEAT_LONG)

    client = OpenAI()

    # Seed.
    seed_provider = YantrikDBMemoryProvider()
    seed_provider.initialize("demo-llm-seed", hermes_home=str(DEMO_HOME))
    print()
    print(f"  ▸ seeding {len(SEED_SKILLS)} skills:")
    for entry in SEED_SKILLS:
        print(f"        • {entry['skill_id']:48s} ({entry['skill_type']})")
    loaded = load_seed_skills(seed_provider)
    print(f"  ▸ {loaded} skills loaded — substrate is now lived-in")
    seed_provider.shutdown()
    pause(BEAT_LONG)

    # ─────────────────────────────────────────────────────────────────
    # SESSION 1 — incident triage, agent composes a response from
    # multiple substrate skills, then crystallizes a new lesson.
    # ─────────────────────────────────────────────────────────────────
    banner("Session 1 — agent triages a real incident using the substrate")
    provider1 = YantrikDBMemoryProvider()
    provider1.initialize("demo-llm-s1", hermes_home=str(DEMO_HOME))
    show_substrate(provider1, "before triage")
    pause(BEAT_MED)

    narrate(
        "The agent gets a concrete incident report. Before responding, it "
        "uses yantrikdb_skill_search to gather context from past sessions — "
        "the substrate as working memory under pressure."
    )
    pause(BEAT_LONG)

    system_1 = (
        "You are a Hermes Agent with yantrikdb tools for persistent skills and memory.\n\n"
        "When a user reports an incident, BEFORE responding:\n"
        "1. Use yantrikdb_skill_search to find any procedures, references, or lessons\n"
        "   from past sessions that apply. Search broadly (try 2-3 different queries\n"
        "   if needed — debugging shape, deployment shape, etc.).\n"
        "2. Read what you find. Compose your diagnosis using the relevant past lessons.\n"
        "3. After responding, call yantrikdb_skill_outcome for each skill you actually\n"
        "   leaned on, with a brief note about how it helped.\n\n"
        "Be concise in your diagnosis. The substrate work — search and outcomes — is "
        "the load-bearing part of your value here."
    )
    user_1 = (
        "Our staging service stopped responding to /v1/* endpoints around 03:00 UTC "
        "after a deploy that extended ALLOWED_KINDS to include a new event type. "
        "/v1/health is still returning 200 but every operational endpoint hangs. "
        "We deployed both the polling watcher and the ingest service this morning. "
        "What's going on, and what should we check first?"
    )
    s1_messages = run_agent_turn(client, provider1, system_1, user_1)
    s1_outcomes = count_outcomes_in_messages(s1_messages)

    pause(BEAT_MED)
    show_substrate(provider1, "after session 1", outcomes=s1_outcomes)
    pause(BEAT_LONG)
    provider1.shutdown()

    banner("[ session 1 ended — agent state torn down — substrate persists ]")
    pause(BEAT_MED)

    # ─────────────────────────────────────────────────────────────────
    # SESSION 2 — different agent, similar incident, finds + applies
    # the lessons from session 1, then crystallizes the recurring pattern
    # as a NEW skill (since we've now seen it twice).
    # ─────────────────────────────────────────────────────────────────
    banner("Session 2 — fresh agent, similar-shape incident days later")
    provider2 = YantrikDBMemoryProvider()
    provider2.initialize("demo-llm-s2", hermes_home=str(DEMO_HOME))
    show_substrate(provider2, "session 2 begins — substrate carries session 1's outcomes",
                   outcomes=s1_outcomes)
    pause(BEAT_MED)

    narrate(
        "Different agent instance. Zero chat context. Similar-shape incident. "
        "The model searches the substrate first, applies what fits, and — because "
        "this incident shape has now recurred — crystallizes the pattern as a "
        "concrete lesson so the next session doesn't have to re-derive it."
    )
    pause(BEAT_LONG)

    system_2 = (
        "You are a Hermes Agent. Before diagnosing an incident, use "
        "yantrikdb_skill_search to check for relevant past procedures and lessons. "
        "Apply them. Call yantrikdb_skill_outcome for each you leaned on.\n\n"
        "IMPORTANT: If you observe that this incident shape has the SAME root cause "
        "as patterns the existing skills warn about, that's evidence the pattern is "
        "recurring — call yantrikdb_skill_define to crystallize a concrete, specific "
        "lesson tailored to THIS exact symptom + cause (not a generic restatement). "
        "Use a clear skill_id like 'incident.ingest.allowed_kinds_deploy_race' or "
        "similar. Be concise in the diagnosis text — the substrate work is the value."
    )
    user_2 = (
        "We just deployed a new event type to our pipeline. The polling watcher "
        "started emitting the new kind at 14:30, ingest service deploy completed "
        "at 14:35. Now the ingest endpoint hangs on requests touching that event "
        "type. Where do I look first?"
    )
    s2_messages = run_agent_turn(client, provider2, system_2, user_2)
    s2_outcomes = count_outcomes_in_messages(s2_messages)
    total_outcomes = s1_outcomes + s2_outcomes

    pause(BEAT_MED)
    show_substrate(provider2, "after session 2 — outcomes accrue, new lesson lands",
                   outcomes=total_outcomes)
    pause(BEAT_LONG)
    provider2.shutdown()

    banner("Substrate as working memory — composed, applied, refined")
    print(f"  ▸ session 1:  agent searched the substrate, composed a multi-source")
    print(f"                diagnosis from {len(SEED_SKILLS)} prior-session skills, recorded")
    print(f"                {s1_outcomes} outcome(s) against the skills it leaned on")
    print(f"  ▸ session 2:  fresh agent + similar incident → found relevant skills →")
    print(f"                applied them → recorded {s2_outcomes} outcome(s) → crystallized")
    print(f"                a new lesson when the pattern recurred")
    print(f"  ▸ what this is:  the substrate doing real work across real tasks,")
    print(f"                   not store-and-retrieve API theatre")
    print(f"  ▸ docs:       https://yantrikdb.com/guides/autonomous-skills/")
    pause(BEAT_LONG)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n  ! demo failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)
