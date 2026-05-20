#!/usr/bin/env python3
"""End-to-end demo of the skill lifecycle through the Hermes plugin.

What this script shows: the SAME tool-call entry point Hermes invokes
when the agent's LLM emits a tool call. We bypass the LLM here — the
"agent" is scripted — so the demo runs deterministically in <60s.
The plugin code, the engine, and the substrate are real.

Lifecycle:
    1. Session 1 — agent observes a useful pattern, calls skill_define
    2. Session 2 (after a restart) — fresh agent searches, finds the
       skill, follows it, records the outcome via skill_outcome
    3. Substrate reflects both events: 1 new skill + 1 outcome row

Designed to be driven by VHS (charmbracelet.com/vhs) — the .tape file
sitting next to this script paces the output for a clean recording.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Make the demo self-contained: fresh, ephemeral home so the dashboard
# state we show is what THIS demo produced, not pre-existing data.
DEMO_HOME = Path(tempfile.mkdtemp(prefix="yantrikdb_hermes_demo_"))
os.environ["YANTRIKDB_DB_PATH"] = str(DEMO_HOME / "memory.db")
os.environ["YANTRIKDB_MODE"] = "embedded"
os.environ["YANTRIKDB_NAMESPACE"] = "demo"
os.environ["YANTRIKDB_SKILLS_ENABLED"] = "true"

# Suppress HuggingFace tqdm noise.
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"


def banner(text: str) -> None:
    width = max(60, len(text) + 4)
    print()
    print("─" * width)
    print(f"  {text}")
    print("─" * width)


def pause(seconds: float = 0.6) -> None:
    sys.stdout.flush()
    time.sleep(seconds)


def main() -> None:
    banner("Step 1 — fresh substrate, 0 skills")
    print(f"  YANTRIKDB_DB_PATH = {DEMO_HOME / 'memory.db'}")
    print(f"  YANTRIKDB_NAMESPACE = demo")
    print(f"  YANTRIKDB_SKILLS_ENABLED = true")
    pause(1.2)

    # Lazy import so the env vars above are picked up.
    from yantrikdb_hermes_plugin import YantrikDBMemoryProvider  # noqa: E402

    provider = YantrikDBMemoryProvider()
    provider.initialize("demo-session-1", hermes_home=str(DEMO_HOME))

    def tool(name: str, **args):
        """Call a plugin tool the way Hermes does (via handle_tool_call)."""
        return provider.handle_tool_call(name, args)

    pause(0.5)
    print()
    print("  $ provider.handle_tool_call('yantrikdb_stats', {'namespace': 'demo'})")
    pause(0.4)
    print("   →", tool("yantrikdb_stats", namespace="demo")[:140], "…")
    pause(1.0)

    banner("Step 2 — Session 1: agent observes a pattern, defines a skill")
    print()
    print("  The agent has just shipped a clean release. It noticed the same")
    print("  sequence worked three times: feature branch → CI → squash-merge")
    print("  → tag → GH release → PyPI verify. It chooses to crystallize.")
    pause(2.5)
    print()
    print("  $ provider.handle_tool_call('yantrikdb_skill_define', { … })")
    pause(0.6)
    result = tool(
        "yantrikdb_skill_define",
        skill_id="workflow.release.yantrikos_repo",
        skill_type="procedure",
        applies_to=["release", "workflow", "yantrikos"],
        body=(
            "For every release on a yantrikos repo with branch protection: "
            "(1) feature branch + PR with CI green on Python 3.11/3.12/3.13/3.14, "
            "(2) squash-merge to main with version-bumped CHANGELOG entry, "
            "(3) tag vX.Y.Z + push, "
            "(4) gh release create vX.Y.Z (fires the gated Publish workflow), "
            "(5) verify https://pypi.org/pypi/<name>/json shows the new version, "
            "(6) close referenced issues with credit + install command in the comment."
        ),
        triggers=["release", "ship", "publish to pypi"],
    )
    print("   →", result[:200])
    pause(2.0)

    print()
    print("  $ provider.handle_tool_call('yantrikdb_stats', {'namespace': 'skill_substrate'})")
    pause(0.4)
    print("   →", tool("yantrikdb_stats", namespace="skill_substrate")[:140], "…")
    pause(1.2)

    banner("Step 3 — simulated session restart (fresh agent state)")
    print()
    print("  Tearing down the agent's in-memory state. The substrate persists.")
    pause(1.5)
    provider.shutdown()
    del provider
    pause(0.8)

    print()
    print("  $ provider = YantrikDBMemoryProvider()   # new instance")
    pause(0.4)
    provider2 = YantrikDBMemoryProvider()
    provider2.initialize("demo-session-2", hermes_home=str(DEMO_HOME))
    print("   → provider ready, substrate has the skill from session 1")
    pause(1.5)

    banner("Step 4 — Session 2: fresh agent searches before acting")
    print()
    print("  The agent gets a new request: 'ship v0.4.13 of the plugin.'")
    print("  Before doing anything, it searches the skill substrate.")
    pause(2.0)
    print()
    print("  $ provider.handle_tool_call('yantrikdb_skill_search', {'query': 'how to ship a release', 'top_k': 3})")
    pause(0.6)

    def tool2(name: str, **args):
        return provider2.handle_tool_call(name, args)

    search_result = tool2("yantrikdb_skill_search", query="how to ship a release", top_k=3)
    print("   →", search_result[:280], "…")
    pause(3.0)

    banner("Step 5 — agent follows the skill, reports outcome")
    print()
    print("  The agent reads the skill body, ships the release following the")
    print("  6-step procedure, succeeds, and records the outcome.")
    pause(2.0)
    print()
    print("  $ provider.handle_tool_call('yantrikdb_skill_outcome', { … })")
    pause(0.6)
    outcome = tool2(
        "yantrikdb_skill_outcome",
        skill_id="workflow.release.yantrikos_repo",
        succeeded=True,
        note="shipped v0.4.13 cleanly — PyPI verified within 3min of tag push",
    )
    print("   →", outcome[:200])
    pause(2.0)

    banner("Done — the autonomy loop closed")
    print()
    print("  • Substrate now holds 1 skill (workflow.release.yantrikos_repo)")
    print("  • Outcome ledger has 1 success row, agent's access_count = 1")
    print("  • Next session's agent will see this skill ranked higher")
    print()
    print("  The plugin's code path you just saw is the same one Hermes invokes")
    print("  when its agent's LLM emits a tool call. The LLM is omitted here for")
    print("  determinism; everything else is the live plugin + live engine.")
    print()
    print("  Plugin: yantrikdb-hermes-plugin v" + getattr(__import__("yantrikdb_hermes_plugin"), "__version__", "0.4.12"))
    print("  Substrate: " + str(DEMO_HOME / "memory.db"))
    pause(3.0)
    provider2.shutdown()

    # Tidy up. Keep the .db around for an optional dashboard snapshot
    # but the temp dir gets removed on subsequent demo runs.
    print()
    print("  (Ephemeral demo home left at " + str(DEMO_HOME) + " for inspection)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n  ! demo failed: {type(e).__name__}: {e}")
        sys.exit(1)
