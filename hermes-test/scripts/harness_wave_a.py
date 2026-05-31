#!/usr/bin/env python3
"""Wave A e2e harness — drives the actual plugin against a real embedded
YantrikDB engine (no Hermes-the-LLM-loop, no docker) so we get true
injected-prompt output in seconds for fast iteration.

Mocked unit tests confirm the plumbing wires correctly; this harness
confirms what an LLM would ACTUALLY see in `system_prompt_block()`
across the three Wave A scenarios.

Uses the same loader pattern as tests/conftest.py — loads the plugin
under `yantrikdb_plugin_under_test` so its internal `import yantrikdb`
correctly resolves to the pip-installed engine, not the plugin source.

Run from repo root:
    python hermes-test/scripts/harness_wave_a.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import types
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_DIR = REPO_ROOT / "yantrikdb"
_PKG = "yantrikdb_plugin_under_test"


# ---- Hermes stubs (mirrors tests/conftest.py) ---------------------------

def _ensure_hermes_stubs() -> None:
    if "agent.memory_provider" in sys.modules:
        return
    sys.modules["agent"] = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider(ABC):
        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs: Any) -> None: ...

        def system_prompt_block(self) -> str: return ""
        def prefetch(self, query: str, *, session_id: str = "") -> str: return ""
        def queue_prefetch(self, query: str, *, session_id: str = "") -> None: return None
        def sync_turn(self, u, a, *, session_id: str = "") -> None: return None

        @abstractmethod
        def get_tool_schemas(self) -> list[dict[str, Any]]: ...

        def handle_tool_call(self, tool_name, args, **kwargs): raise NotImplementedError
        def shutdown(self) -> None: return None
        def on_turn_start(self, n, m, **kw): return None
        def on_session_end(self, msgs): return None
        def on_pre_compress(self, msgs) -> str: return ""
        def on_delegation(self, t, r, *, child_session_id="", **kw): return None
        def get_config_schema(self) -> list[dict[str, Any]]: return []
        def save_config(self, v, h): return None
        def on_memory_write(self, a, t, c): return None

    mp_mod.MemoryProvider = MemoryProvider
    sys.modules["agent.memory_provider"] = mp_mod

    sys.modules["tools"] = types.ModuleType("tools")
    registry_mod = types.ModuleType("tools.registry")
    registry_mod.tool_error = lambda message: json.dumps({"error": message})
    sys.modules["tools.registry"] = registry_mod


def _load_plugin():
    _ensure_hermes_stubs()
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules[_PKG] = pkg

    for sub in ("client", "embedded"):
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{sub}", str(PLUGIN_DIR / f"{sub}.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{sub}"] = mod
        spec.loader.exec_module(mod)

    init_spec = importlib.util.spec_from_file_location(
        _PKG, str(PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    provider_mod = importlib.util.module_from_spec(init_spec)
    sys.modules[_PKG] = provider_mod
    init_spec.loader.exec_module(provider_mod)
    return provider_mod


# ---- Test harness -------------------------------------------------------

def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def block(label: str, content: str) -> None:
    print(f"\n--- {label} ---")
    print(content if content else "(empty)")
    print(f"--- end {label} ({len(content)} chars) ---")


def wait_prefetch(provider) -> None:
    if provider._prefetch_thread and provider._prefetch_thread.is_alive():
        provider._prefetch_thread.join(timeout=30.0)


def main() -> int:
    os.environ["YANTRIKDB_MODE"] = "embedded"
    os.environ["YANTRIKDB_SKILLS_ENABLED"] = "true"
    os.environ["YANTRIKDB_AUTO_SKILL_ATTACH"] = "true"
    os.environ["YANTRIKDB_SURFACE_PENDING_CONFLICTS"] = "true"
    os.environ["YANTRIKDB_AUTO_THINK_ON_SESSION_END"] = "false"

    plugin = _load_plugin()
    home = Path(tempfile.mkdtemp(prefix="wave-a-e2e-"))
    print(f"isolated HERMES_HOME = {home}")

    provider = plugin.YantrikDBMemoryProvider()
    try:
        provider.initialize(
            "sess-test",
            agent_workspace="wave-a-e2e",
            agent_identity="harness",
            platform="cli",
            hermes_home=str(home),
        )
    except Exception as e:
        print(f"FAIL: initialize() raised: {e}")
        shutil.rmtree(home, ignore_errors=True)
        return 1

    if provider._client is None:
        print(f"FAIL: provider._client is None. init_error={provider._init_error}")
        shutil.rmtree(home, ignore_errors=True)
        return 1

    client = provider._client
    print(f"backend={type(client).__name__}  namespace={provider._namespace}")

    results = []

    # ---- Scenario 0: empty substrate baseline -----------------------------
    banner("Scenario 0 — empty substrate baseline")
    baseline = provider.system_prompt_block()
    block("system_prompt_block (empty substrate)", baseline)
    if "Active skill" in baseline or "Pending contradictions" in baseline:
        print("FAIL: Wave A blocks appeared on empty substrate")
        results.append(("S0", False, "spurious injection"))
    else:
        print("PASS: clean baseline, no spurious injection")
        results.append(("S0", True, "clean baseline"))

    # ---- Scenario 1: A1 recall auto-injection -----------------------------
    banner("Scenario 1 — A1 recall auto-injection")
    client.remember(
        "Pranab prefers minimal commit messages with no trailing summary",
        namespace=provider._namespace, importance=0.8,
    )
    client.remember(
        "The kitchen counter is granite",
        namespace=provider._namespace, importance=0.5,
    )
    time.sleep(0.5)

    provider.queue_prefetch(
        "what's Pranab's commit message style", session_id="sess-test",
    )
    wait_prefetch(provider)
    recall_block = provider.prefetch("dummy", session_id="sess-test")
    block("prefetch() output (what Hermes injects pre-turn)", recall_block)
    if "commit" in recall_block.lower():
        print("PASS: recall surfaced the relevant memory")
        results.append(("S1 A1 recall", True, "matched"))
    else:
        print("CHECK: recall didn't surface — score threshold or embedding mismatch")
        results.append(("S1 A1 recall", False, "no match"))

    # ---- Scenario 2: A2 skill auto-attach ---------------------------------
    banner("Scenario 2 — A2 skill auto-attach")
    # Use on_conflict=replace so re-running the harness is idempotent
    # (the engine's cache_dir may carry state across temp homes).
    try:
        client.skill_define(
            skill_id="git.commit_clean",
            body=(
                "Always rebase before merge so history stays linear and reviewable. "
                "No co-author tags. No marketing voice in commit messages."
            ),
            skill_type="procedure",
            applies_to=["git", "workflow"],
            on_conflict="replace",
        )
        print("seeded skill: git.commit_clean")
    except Exception as e:
        print(f"FAIL: skill_define raised: {e}")
        return 1

    provider.queue_prefetch(
        "how should I structure my git commits", session_id="sess-test",
    )
    wait_prefetch(provider)
    prompt_with_skill = provider.system_prompt_block()
    block("system_prompt_block (after skill_search match)", prompt_with_skill)
    if "Active skill" in prompt_with_skill and "git.commit_clean" in prompt_with_skill:
        print("PASS: A2 surfaced the matching skill in the prompt")
        results.append(("S2 A2 skill auto-attach", True, "surfaced"))
    else:
        print(
            "CHECK: A2 didn't surface — check auto_skill_min_score "
            "(default 0.55) vs actual match score"
        )
        results.append(("S2 A2 skill auto-attach", False, "no surface"))

    second = provider.system_prompt_block()
    if "Active skill" in second:
        print("FAIL: A2 echoed skill across two consecutive calls (should drain)")
        results.append(("S2 A2 drain", False, "echoed"))
    else:
        print("PASS: A2 drained after first read (single-turn surface)")
        results.append(("S2 A2 drain", True, "drained"))

    # ---- Scenario 3: A3 pending-conflict surface --------------------------
    banner("Scenario 3 — A3 pending-conflict surface")
    client.remember(
        "Pranab prefers tabs in Python",
        namespace=provider._namespace, importance=0.7,
    )
    client.remember(
        "Pranab prefers spaces in Python",
        namespace=provider._namespace, importance=0.7,
    )
    time.sleep(0.3)
    try:
        think_resp = client.think(
            namespace=provider._namespace, run_pattern_mining=False,
        )
        print(
            f"think() ran: conflicts_found={think_resp.get('conflicts_found', '?')}"
        )
    except Exception as e:
        print(f"WARN: think() raised: {e}")

    provider._pending_conflicts_last_poll = 0.0
    provider.queue_prefetch("python style decision", session_id="sess-test")
    wait_prefetch(provider)
    prompt_with_conflict = provider.system_prompt_block()
    block("system_prompt_block (after conflict surface)", prompt_with_conflict)
    if "Pending contradictions" in prompt_with_conflict:
        print("PASS: A3 surfaced unresolved conflict")
        results.append(("S3 A3 conflict surface", True, "surfaced"))
    elif provider._pending_conflicts:
        print(
            f"PARTIAL: conflicts in cache but not in prompt: "
            f"{provider._pending_conflicts}"
        )
        results.append(("S3 A3 conflict surface", False, "cached, not surfaced"))
    else:
        print("CHECK: think() didn't detect conflict — engine behavior, not Wave A")
        results.append(("S3 A3 conflict surface", False, "no engine detection"))

    # ---- Summary ----------------------------------------------------------
    banner("Wave A harness — summary")
    for name, ok, note in results:
        print(f"  {'PASS' if ok else 'FAIL'} — {name} ({note})")
    print(f"\nisolated home preserved at: {home}")
    print("rm -rf the dir above to clean up.")
    provider.shutdown()
    return 0 if all(ok for _, ok, _ in results) else 2


if __name__ == "__main__":
    sys.exit(main())
