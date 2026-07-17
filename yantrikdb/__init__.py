"""YantrikDB memory plugin — self-maintaining memory for Hermes.

YantrikDB is a memory provider that actively maintains what it stores
instead of behaving like an append-only vector index:

- ``think()`` runs a bounded maintenance pass — canonicalizes duplicates,
  flags superseded or contradictory facts, and mines co-occurrence
  patterns.
- Contradiction tracking surfaces conflicting claims for the agent to
  resolve explicitly (``resolve_conflict``) rather than overwriting
  silently.
- Recency-aware ranking prefers fresher facts over stale ones without
  deleting the older records — every result is annotated with a
  ``why_retrieved`` reason list so recall is explainable.
- A knowledge graph promotes related memories in recall through entity
  edges created via ``relate``.

The plugin is a thin HTTP client against an externally managed
``yantrikdb-server`` (see README for deployment). The plugin never
starts, stops, or upgrades the backend — same pattern as ``honcho``.

Config via env + $HERMES_HOME/yantrikdb.json:
  YANTRIKDB_URL              — default http://localhost:7438
  YANTRIKDB_TOKEN            — required, Bearer token from `yantrikdb token create`
  YANTRIKDB_NAMESPACE        — default "hermes"; combined with agent_workspace:agent_identity
  YANTRIKDB_TOP_K            — default 10
  YANTRIKDB_OWNER_SCOPING    — optional; if true, append resolved-owner shard to namespace
  YANTRIKDB_IDENTITY_MAP_PATH — optional JSON actor->owner alias map
  YANTRIKDB_READ_TIMEOUT     — default 15.0 seconds
  YANTRIKDB_CONNECT_TIMEOUT  — default 5.0 seconds
  YANTRIKDB_RETRY_TOTAL      — default 3 retries on transient 5xx
  YANTRIKDB_MAX_TEXT_LEN     — default 25000 chars; text is truncated client-side above this
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

try:
    from agent.memory_provider import MemoryProvider
    from tools.registry import tool_error
    _HERMES_AVAILABLE = True
except ImportError:
    # Plugin is being imported outside a Hermes runtime — typically because
    # `yantrikdb-hermes-plugin` was pip-installed and the user is invoking
    # the CLI installer (`yantrikdb-hermes install <hermes_root>`). The
    # provider class never runs in this path; we just need module load to
    # succeed so the CLI's `from yantrikdb_hermes_plugin.cli import main`
    # works. Inside Hermes, the real imports take over.
    _HERMES_AVAILABLE = False

    class MemoryProvider:  # type: ignore[no-redef]
        """Stub used only when the plugin is imported outside Hermes.

        Real `agent.memory_provider.MemoryProvider` is an ABC; this stub is
        a plain class so the YantrikDBMemoryProvider subclass below still
        defines successfully when Hermes isn't on the import path. Hermes
        instantiates the provider via filesystem-loaded source (a fresh
        import from `plugins/memory/yantrikdb/`), so this stub is never
        the one Hermes sees in practice.
        """

    def tool_error(message: str) -> str:  # type: ignore[no-redef]
        # Stub for non-Hermes import path. The provider class never runs
        # here; the real tool_error below shadows it for Hermes execution.
        import json
        return json.dumps({"error": message})

from .client import (
    DEFAULT_NAMESPACE,
    YantrikDBAuthError,
    YantrikDBClient,
    YantrikDBClientError,
    YantrikDBConfig,
    YantrikDBError,
    YantrikDBServerError,
    YantrikDBTransientError,
)
from .embedded import make_backend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured tool envelope (v0.4.16+)
# ---------------------------------------------------------------------------
#
# Every tool response from this plugin carries an unambiguous status signal so
# an LLM later asked "what did I just do?" can't confabulate success on a
# silent failure. Pattern documented by yantrikdb-agi (rid 019e6c27) after a
# real incident: a tool call failed during a YDB cluster restart, the agent's
# narrative LLM described success that did not happen.
#
# Envelope fields (always present on every tool response):
#   status:  "ok" | "failed"       — primary LLM-readable signal
#   ok:      true | false           — primary machine-readable signal
#   tool:    "yantrikdb_<name>"    — tool that produced this result
#   ts:      epoch seconds          — temporal grounding
#
# Failure responses additionally carry `error` (the message; legacy key kept
# for back-compat) and `reason` (alias). Success responses preserve all
# tool-specific keys verbatim (`rid`, `stored`, `results`, etc.) so existing
# agent code that reads those keys continues to work unchanged.
#
# Shadow the imported `tool_error` so every error response carries the
# envelope, regardless of which `tools.registry` shipped with the host.


def tool_error(message: str, *, tool: str = "") -> str:  # type: ignore[no-redef]  # noqa: F811
    """Structured error envelope. Replaces the imported `tool_error`.

    The legacy `{"error": message}` shape is preserved (additive only) so any
    agent code that scans for the `error` key keeps working.
    """
    payload: dict[str, Any] = {
        "status": "failed",
        "ok": False,
        "ts": time.time(),
        "error": message,
        "reason": message,
    }
    if tool:
        payload["tool"] = tool
    return json.dumps(payload)


def _envelope_ok(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a success envelope around tool-specific payload keys."""
    return {
        "status": "ok",
        "ok": True,
        "tool": tool,
        "ts": time.time(),
        **payload,
    }


def _wrap_dispatch(tool: str, raw_json: str) -> str:
    """Wrap a `_do_*` method's JSON return in the structured envelope.

    `_do_*` methods either return a success-shaped JSON (e.g. `{"rid":...}`)
    or call `tool_error()` directly (already enveloped). This helper:

    - Parses the JSON. On parse failure, returns a structured error envelope
      so the LLM still gets unambiguous failure signal instead of broken JSON.
    - If the dict already carries `status` / `ok` (already enveloped, e.g.
      from a direct `tool_error()` call), passes through unchanged.
    - Otherwise wraps as success — preserves all original keys, adds the
      envelope fields.
    """
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return tool_error(
            f"plugin produced non-JSON tool response: {str(raw_json)[:200]}",
            tool=tool,
        )
    if not isinstance(data, dict):
        return tool_error(
            f"plugin produced non-dict tool response: {type(data).__name__}",
            tool=tool,
        )
    if "status" in data and "ok" in data:
        # Already enveloped (e.g. _do_* called tool_error directly).
        # If the tool field is missing, fill it in.
        if "tool" not in data and tool:
            data["tool"] = tool
            return json.dumps(data)
        return raw_json
    return json.dumps(_envelope_ok(tool, data))

# Circuit breaker — after N consecutive transient/server/auth failures, the
# plugin short-circuits for _BREAKER_COOLDOWN seconds so a flapping server
# does not hammer Hermes' event loop. 4xx errors are deterministic caller
# mistakes and do NOT count toward the breaker. Matches mem0's 5/120s.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN = 120.0

_PREFETCH_JOIN_SECS = 3.0
_SYNC_JOIN_SECS = 5.0
_SESSION_END_JOIN_SECS = 10.0


# ---------------------------------------------------------------------------
# Tool schemas
#
# Framing: these are memory maintenance operations, not opaque cognition.
# Descriptions tell the agent when to reach for each tool, what the tool
# will mutate, and how to read its structured result.
# ---------------------------------------------------------------------------

REMEMBER_SCHEMA = {
    "name": "yantrikdb_remember",
    "description": (
        "Store a durable memory. Use for decisions, preferences, facts about "
        "people, and project context. Skip ephemeral task state and anything "
        "derivable from code or git. Text over 25k characters is truncated "
        "client-side with a visible marker."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The fact or decision to remember, as a complete sentence.",
            },
            "importance": {
                "type": "number",
                "description": (
                    "0.0-1.0 resistance to temporal ranking decay. "
                    "0.8+ for critical decisions, 0.5-0.7 for useful context, "
                    "0.3-0.5 for background detail. Default 0.6."
                ),
            },
            "domain": {
                "type": "string",
                "description": (
                    "Optional tag for filtered recall: 'work', 'preference', "
                    "'people', 'architecture', 'infrastructure', etc."
                ),
            },
        },
        "required": ["text"],
    },
}

RECALL_SCHEMA = {
    "name": "yantrikdb_recall",
    "description": (
        "Explainable recall — semantic search ranked by relevance × recency × "
        "importance with knowledge-graph boosting. Each result includes a "
        "`why_retrieved` list (semantic_match / graph-connected / "
        "keyword_match / important / emotionally weighted, etc.) so the "
        "agent can see why each memory ranked. Call before making claims "
        "about the user or past decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query. Be specific.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max results (default 10, capped at 50).",
            },
            "domain": {
                "type": "string",
                "description": "Optional domain filter (e.g. 'work').",
            },
            "since": {
                "type": "string",
                "description": (
                    "v0.5+: time-aware filter — only return memories created "
                    "at-or-after this point. Accepts ISO timestamps "
                    "(2026-05-29T00:00:00Z, 2026-05-29) or relative shorthand "
                    "(today, yesterday, last week, 7d, 24h). Combine with "
                    "`until` for a window."
                ),
            },
            "until": {
                "type": "string",
                "description": (
                    "v0.5+: time-aware filter — only return memories created "
                    "before this point. Same formats as `since`."
                ),
            },
            "include_candidates": {
                "type": "boolean",
                "description": (
                    "v0.5+: include source=extracted candidate facts "
                    "(certainty<=0.4) in results. Default false — "
                    "candidates are hidden until think() promotes them."
                ),
            },
            "reinforce": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "v0.6+: list of rids (from a PRIOR recall) that proved "
                    "useful. Reinforces them so future recalls rank them "
                    "higher. Only takes effect when self-tuning recall is "
                    "enabled (YANTRIKDB_SELF_TUNING_RECALL=true); otherwise "
                    "ignored. Pass the rids you actually relied on after "
                    "acting on a recall result."
                ),
            },
        },
        "required": ["query"],
    },
}

FORGET_SCHEMA = {
    "name": "yantrikdb_forget",
    "description": (
        "Tombstone (soft-delete) a memory by its rid. Use when the user "
        "retracts a fact or you are resolving a conflict by dropping the "
        "obsolete record. Tombstoned memories stop surfacing in recall but "
        "remain for audit."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rid": {
                "type": "string",
                "description": "Memory id returned by yantrikdb_recall or yantrikdb_remember.",
            },
        },
        "required": ["rid"],
    },
}

THINK_SCHEMA = {
    "name": "yantrikdb_think",
    "description": (
        "Run a bounded memory maintenance pass: canonicalize near-duplicates, "
        "flag contradictory or superseded facts, optionally mine co-occurrence "
        "patterns. Returns structured counts plus a `triggers` list suggesting "
        "follow-up actions. Call at natural break points (end of a project "
        "phase, a long user pause) — it is the most expensive operation and "
        "should not run per turn. This is YantrikDB's differentiating feature."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "consolidation_limit": {
                "type": "integer",
                "description": "Max memories to consolidate this run (default: server-configured).",
            },
            "run_pattern_mining": {
                "type": "boolean",
                "description": "Also mine temporal and co-occurrence patterns. Default false.",
            },
        },
        "required": [],
    },
}

CONFLICTS_SCHEMA = {
    "name": "yantrikdb_conflicts",
    "description": (
        "List open contradictions detected by yantrikdb_think. Each conflict "
        "includes the two (or more) memory ids, a detection reason, and a "
        "priority. YantrikDB never silently overwrites — contradictions "
        "surface here until the agent resolves them explicitly."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

RESOLVE_CONFLICT_SCHEMA = {
    "name": "yantrikdb_resolve_conflict",
    "description": (
        "Resolve a contradiction from yantrikdb_conflicts. Strategies: "
        "'keep_winner' (pick the winner_rid, tombstone the other), "
        "'merge' (emit a merged new_text, tombstone both), "
        "'keep_both' (record both as context-dependent), "
        "'dismiss' (close without action, e.g. false positive)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conflict_id": {"type": "string", "description": "Id from yantrikdb_conflicts."},
            "strategy": {
                "type": "string",
                "description": "One of: keep_winner, merge, keep_both, dismiss.",
            },
            "winner_rid": {
                "type": "string",
                "description": "Required for 'keep_winner'. The rid to preserve.",
            },
            "new_text": {
                "type": "string",
                "description": "Required for 'merge'. The consolidated text that replaces both.",
            },
            "resolution_note": {
                "type": "string",
                "description": "Optional audit note explaining the choice.",
            },
        },
        "required": ["conflict_id", "strategy"],
    },
}

RELATE_SCHEMA = {
    "name": "yantrikdb_relate",
    "description": (
        "Record a relationship edge between two entities in the knowledge "
        "graph (e.g. 'Alice works_at Acme', 'ProjectX uses React'). "
        "Edges boost recall — memories near related entities rank higher "
        "and show `graph-connected via X` in their why_retrieved reasons."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "Source entity."},
            "target": {"type": "string", "description": "Target entity."},
            "relationship": {
                "type": "string",
                "description": (
                    "Relationship verb, snake_case by convention "
                    "(e.g. 'works_at', 'uses', 'reports_to')."
                ),
            },
        },
        "required": ["entity", "target", "relationship"],
    },
}

STATS_SCHEMA = {
    "name": "yantrikdb_stats",
    "description": (
        "Operational snapshot: active memory count, tombstoned count, "
        "graph edges, entities, open conflicts, pending triggers. Use to "
        "decide whether to call yantrikdb_think, spot runaway growth, or "
        "see whether conflicts have accumulated."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

# -- Trigger consumer tools (v0.4.13+) --------------------------------
#
# yantrikdb_think produces pending triggers as a side effect — flags
# the engine raises when it spots a conflict, a stale-by-policy memory,
# or a pattern that may warrant agent attention. Until they're closed
# they stay on the pending queue; ``yantrikdb_stats.pending_triggers``
# counts them. These four tools are the consumer side of that loop:
# inspect the queue, then acknowledge / dismiss / act on each entry.
#
# Lifecycle: ``acknowledge`` records "agent saw this," ``dismiss`` is
# "declined to act," ``act_on`` is "took action in response." All three
# close the trigger; ``act_on`` adds it to ``get_trigger_history`` as
# an audit-trail event the substrate can later mine.

PENDING_TRIGGERS_SCHEMA = {
    "name": "yantrikdb_pending_triggers",
    "description": (
        "List triggers waiting for agent consumption. Triggers are "
        "produced by yantrikdb_think (conflict-detected, stale-memory, "
        "pattern-noticed signals) and accumulate until the agent closes "
        "them via acknowledge / dismiss / act_on. Check this when "
        "yantrikdb_stats.pending_triggers is non-zero, or at session "
        "start as a backlog scan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max triggers to return (default 10).",
            },
        },
        "required": [],
    },
}

ACKNOWLEDGE_TRIGGER_SCHEMA = {
    "name": "yantrikdb_acknowledge_trigger",
    "description": (
        "Mark a trigger as seen by the agent — no follow-up action "
        "recorded, but the trigger leaves the pending queue. Use when "
        "you've read the signal and decided no further work is needed "
        "(e.g., low-priority pattern, already-handled conflict)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_id": {
                "type": "string",
                "description": "Id from yantrikdb_pending_triggers.",
            },
        },
        "required": ["trigger_id"],
    },
}

DISMISS_TRIGGER_SCHEMA = {
    "name": "yantrikdb_dismiss_trigger",
    "description": (
        "Close a trigger as a non-issue (false positive or out-of-scope). "
        "Distinct from acknowledge_trigger: dismiss signals the trigger "
        "shouldn't have fired or doesn't merit attention; acknowledge "
        "is closer to 'noted, moving on'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_id": {
                "type": "string",
                "description": "Id from yantrikdb_pending_triggers.",
            },
        },
        "required": ["trigger_id"],
    },
}

ACT_ON_TRIGGER_SCHEMA = {
    "name": "yantrikdb_act_on_trigger",
    "description": (
        "Record that the agent took action in response to a trigger. "
        "Use after actually doing something — calling resolve_conflict, "
        "running think() with new params, updating a stale memory. The "
        "action itself happens via other tools; this records the "
        "outcome so the substrate's trigger history reflects reality."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "trigger_id": {
                "type": "string",
                "description": "Id from yantrikdb_pending_triggers.",
            },
        },
        "required": ["trigger_id"],
    },
}

# -- Skills (v0.3.0+) -------------------------------------------------
#
# Skills are procedural memory: reusable patterns the agent distills
# from observed success and pulls back next session. They live in the
# shared ``skill_substrate`` namespace alongside skills authored by
# other YantrikDB consumers — Hermes-authored entries are tagged
# ``metadata.source=hermes`` so any consumer can filter Hermes-authored
# skills in or out cleanly. Outcomes are append-only; success/failure
# rollup is the agent's pedagogy decision, not the substrate's.
#
# Note: this is a peer surface to the filesystem skills Hermes already
# has at $HERMES_HOME/skills/. Filesystem = human-authored, durable,
# version-controlled. YantrikDB skills = agent-authored, runtime-
# evolving, semantic-search-queryable. Different kinds of canonical;
# the model picks the right substrate by lifecycle, not by competition.

SKILL_SEARCH_SCHEMA = {
    "name": "yantrikdb_skill_search",
    "description": (
        "Semantic search over agent-authored skills in the shared "
        "skill_substrate. Use to find a procedural pattern relevant to "
        "the current task before acting — agent-authored skills capture "
        "what worked in previous similar situations. Distinct from "
        "yantrikdb_recall (episodic/semantic memory): skills are "
        "procedures, memories are facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of the task or pattern.",
            },
            "top_k": {
                "type": "integer",
                "description": "Max results (default 10).",
            },
            "applies_to": {
                "type": "string",
                "description": (
                    "Optional filter: only return skills tagged for this "
                    "context (e.g. 'git', 'deploy', 'pgsql')."
                ),
            },
        },
        "required": ["query"],
    },
}

SKILL_DEFINE_SCHEMA = {
    "name": "yantrikdb_skill_define",
    "description": (
        "Distill a procedural pattern into a reusable skill. Use when "
        "you've observed a sequence that worked and want it findable next "
        "session. The body should be a self-contained instruction — "
        "future you (or another agent) reads it and follows it. Skill "
        "naming convention: dot-separated lowercase, e.g. "
        "'git.commit_clean', 'deploy.rolling_restart'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": (
                    "Dot-separated lowercase identifier matching "
                    "^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)+$, length 4-200. "
                    "First segment is the broad category, later segments "
                    "narrow it (e.g. 'workflow.git.commit_clean')."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "The procedural instructions, 50-5000 chars. Self-"
                    "contained — assume the reader has no prior context."
                ),
            },
            "skill_type": {
                "type": "string",
                "enum": ["procedure", "reference", "lesson", "pattern", "rule"],
                "description": (
                    "procedure: do these steps. reference: information to "
                    "consult. lesson: things to remember from a past mistake. "
                    "pattern: recognizable shape. rule: invariant to maintain."
                ),
            },
            "applies_to": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Tags identifying when this skill is relevant. Lowercase "
                    "+ digits + underscores ONLY (no hyphens, no dots, no "
                    "spaces): each entry must match ^[a-z][a-z0-9_]*$. "
                    "Max 10 entries. Examples: ['git', 'commit'], "
                    "['postgres', 'migration', 'rollback']."
                ),
            },
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional natural-language phrases that should activate this skill.",
            },
            "version": {
                "type": "string",
                "description": "Optional version string (e.g. '1.0.0').",
            },
            "supersedes_skill_id": {
                "type": "string",
                "description": "Optional skill_id this skill replaces.",
            },
        },
        "required": ["skill_id", "body", "skill_type", "applies_to"],
    },
}

SKILL_OUTCOME_SCHEMA = {
    "name": "yantrikdb_skill_outcome",
    "description": (
        "Record whether a skill succeeded when used. Append-only: each "
        "call adds an event to the outcome log; success/failure rollup "
        "is the agent's call, not the substrate's. Use after attempting "
        "a skill to feed back signal for future search ranking."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "The skill_id that was applied.",
            },
            "succeeded": {
                "type": "boolean",
                "description": "True if the skill produced the intended outcome.",
            },
            "note": {
                "type": "string",
                "description": "Optional context — what worked, what didn't, what to adjust.",
            },
        },
        "required": ["skill_id", "succeeded"],
    },
}

OBSERVABILITY_SCHEMA = {
    "name": "yantrikdb_observability",
    "description": (
        "v0.5 Wave C — one-call substrate health snapshot. Aggregates "
        "stats, recent extraction activity, conflict counts, breaker "
        "state, recent skill activity. Use to answer 'how is my memory "
        "doing' without 6 separate tool calls. Returns engine counters, "
        "extractor pattern breakdown, recent-skill list, plus a "
        "human-readable summary line at top."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "namespace": {
                "type": "string",
                "description": "Optional namespace to scope to. "
                              "Defaults to the provider's active namespace.",
            },
        },
        "required": [],
    },
}

EXTRACTION_STATS_SCHEMA = {
    "name": "yantrikdb_extraction_stats",
    "description": (
        "v0.5 Wave B — per-extractor counts of candidate facts auto-extracted "
        "from conversation turns. Use to tune or disable noisy patterns: "
        "low precision (many candidates, few promoted) means the pattern "
        "is over-eager and should have stricter regex or be turned off via "
        "config. Returns counts grouped by extractor pattern, with a "
        "lightweight 'promoted' indicator (candidates whose canonical text "
        "is also stored as a non-candidate inference record)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "namespace": {
                "type": "string",
                "description": "Optional namespace to scope the count to. "
                              "Defaults to the provider's active namespace.",
            },
        },
        "required": [],
    },
}

HYGIENE_SCHEMA = {
    "name": "yantrikdb_hygiene",
    "description": (
        "Proactive memory hygiene. `action=\"scan\"` (default) returns a "
        "digest of cleanup opportunities: open contradictions, engine "
        "counters (active / consolidated / tombstoned), `stale_candidates` "
        "(v0.7 — low-importance, cold/rarely-recalled memories from the "
        "engine's own access stats), and `low_usefulness` (memories that "
        "keep surfacing in recall but were never reinforced). "
        "`action=\"apply\"` acts on them: pass `consolidate=true` to run a "
        "canonicalization pass that merges duplicates, and/or "
        "`forget_rids=[...]` to delete specific memories. Use scan to decide, "
        "apply to clean up. Forgetting is permanent — only forget rids you "
        "(or the user) have confirmed are stale or wrong."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["scan", "apply"],
                "description": "scan (default) to inspect, apply to act.",
            },
            "consolidate": {
                "type": "boolean",
                "description": (
                    "apply only: run a think() consolidation pass to merge "
                    "near-duplicate memories. Default false."
                ),
            },
            "forget_rids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "apply only: rids to permanently forget. Each is "
                    "tombstoned individually; the response reports which "
                    "were found."
                ),
            },
            "namespace": {
                "type": "string",
                "description": "Optional namespace to scope to. "
                              "Defaults to the provider's active namespace.",
            },
        },
        "required": [],
    },
}

KNOWLEDGE_GAPS_SCHEMA = {
    "name": "yantrikdb_knowledge_gaps",
    "description": (
        "v0.7 — the substrate's known unknowns. Returns queries that were "
        "asked often (>= min_count) but answered poorly (average top recall "
        "score <= max_avg_top_score) — a direct signal of what your memory "
        "is MISSING. Use it to decide what to research, ask the user about, "
        "or write down. Scoped to the active namespace on engine 0.9.3+ "
        "(global on 0.9.0-0.9.2). "
        "Returns 'not available' on engines/servers older than 0.9.0."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "min_count": {
                "type": "integer",
                "description": "Minimum times a query must have been asked to "
                               "count as demand. Default 3.",
            },
            "max_avg_top_score": {
                "type": "number",
                "description": "Only surface queries whose average top recall "
                               "score is at or below this (poorly answered). "
                               "Default 0.4.",
            },
            "limit": {
                "type": "integer",
                "description": "Max gaps to return. Default 20.",
            },
        },
        "required": [],
    },
}

RECENT_TURNS_SCHEMA = {
    "name": "yantrikdb_recent_turns",
    "description": (
        "v0.7 — the verbatim recent-conversation buffer (bounded last-N "
        "turns), recorded automatically each turn and kept VERBATIM. It "
        "survives Hermes compression, so use it to recover exactly what was "
        "just said when the semantic store only kept the gist. Optional "
        "`limit` (default 10). Pass `clear=true` to wipe the buffer for the "
        "current namespace. Returns 'not available' on engines/servers "
        "older than 0.9.0."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max turns to return (default 10).",
            },
            "clear": {
                "type": "boolean",
                "description": "If true, clear the buffer for this namespace "
                               "instead of reading it.",
            },
            "namespace": {
                "type": "string",
                "description": "Optional namespace. Defaults to the active one.",
            },
        },
        "required": [],
    },
}

TASKS_SCHEMA = {
    "name": "yantrikdb_tasks",
    "description": (
        "v0.7 — a durable, namespace-scoped task/chore store kept IN the "
        "memory substrate (persists across sessions). Unlike an ephemeral "
        "host TODO list and unlike engine-generated triggers, these are "
        "agent-authored tasks with status + priority + optional subtasks. "
        "`action=\"list\"` (default) returns tasks (optional `status` "
        "filter); `add` creates one (`title` required; optional `priority` "
        "low/medium/high, `parent_id` for a subtask); `update` changes the "
        "`status`/`priority` of `task_id`; `delete` removes `task_id`; "
        "`get` fetches `task_id`. Needs yantrikdb>=0.9.0."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "update", "delete", "get"],
                "description": "list (default) / add / update / delete / get.",
            },
            "title": {
                "type": "string",
                "description": "add: the task title (required for add).",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "add/update: task priority. Default medium.",
            },
            "parent_id": {
                "type": "string",
                "description": "add: parent task id to create a subtask.",
            },
            "task_id": {
                "type": "string",
                "description": "update/delete/get: the target task id.",
            },
            "status": {
                "type": "string",
                "description": "list: filter by status. update: new status "
                               "(e.g. open / in_progress / done).",
            },
            "namespace": {
                "type": "string",
                "description": "Optional namespace. Defaults to the active one.",
            },
        },
        "required": [],
    },
}

ALL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    REMEMBER_SCHEMA,
    RECALL_SCHEMA,
    FORGET_SCHEMA,
    THINK_SCHEMA,
    CONFLICTS_SCHEMA,
    RESOLVE_CONFLICT_SCHEMA,
    RELATE_SCHEMA,
    STATS_SCHEMA,
    PENDING_TRIGGERS_SCHEMA,
    ACKNOWLEDGE_TRIGGER_SCHEMA,
    DISMISS_TRIGGER_SCHEMA,
    ACT_ON_TRIGGER_SCHEMA,
    SKILL_SEARCH_SCHEMA,
    SKILL_DEFINE_SCHEMA,
    SKILL_OUTCOME_SCHEMA,
    EXTRACTION_STATS_SCHEMA,
    OBSERVABILITY_SCHEMA,
    HYGIENE_SCHEMA,
    KNOWLEDGE_GAPS_SCHEMA,
    RECENT_TURNS_SCHEMA,
    TASKS_SCHEMA,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_engine_cache_dir() -> None:
    """Pre-create the engine's model-cache directory if it doesn't exist.

    The yantrikdb engine downloads bundled-named embedders to
    ``dirs::cache_dir()/yantrikdb/models/`` — on Linux that resolves via
    ``$XDG_CACHE_HOME`` or ``$HOME/.cache``. On a vanilla install the engine
    auto-creates it; in environments where the parent dir doesn't exist or
    where ``dirs::cache_dir()`` returns ``None`` (HOME unset, or strict
    sandboxing), the auto-create can fail silently and the embedder-attach
    call raises later inside ``set_embedder_named``.

    We side-step that by ensuring the candidate paths exist before any
    download path runs. Failure here is non-fatal — if we can't create the
    dir, the engine will surface its own error message and ``initialize()``
    will capture it as ``self._init_error``.
    """
    candidates: list[Path] = []
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        candidates.append(Path(xdg) / "yantrikdb" / "models")
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home:
        candidates.append(Path(home) / ".cache" / "yantrikdb" / "models")
    with contextlib.suppress(RuntimeError, OSError):
        candidates.append(Path.home() / ".cache" / "yantrikdb" / "models")
    for p in candidates:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.debug("Could not pre-create engine cache dir %s: %s", p, e)



def _identity_actor_id(kwargs: dict[str, Any]) -> str:
    """Return the raw platform actor id Hermes threaded into provider init."""
    platform = (kwargs.get("platform") or "cli").strip() or "cli"
    raw_user = (
        kwargs.get("user_id")
        or kwargs.get("user_name")
        or kwargs.get("agent_identity")
        or "default"
    )
    return f"{platform}:{str(raw_user).strip() or 'default'}"


def _conversation_id(kwargs: dict[str, Any]) -> str | None:
    platform = (kwargs.get("platform") or "cli").strip() or "cli"
    chat_id = (kwargs.get("chat_id") or "").strip()
    thread_id = (kwargs.get("thread_id") or "").strip()
    if not chat_id:
        return None
    return f"{platform}:{chat_id}:{thread_id}" if thread_id else f"{platform}:{chat_id}"


def _safe_namespace_part(value: str) -> str:
    """Stable, collision-resistant namespace shard for owner ids.

    The shard preserves the first 32 chars of the original identifier
    as a debuggable slug (lowercased, non-[a-z0-9_-] stripped) plus a
    sha256-12 suffix for uniqueness. The slug carries the identifier
    substring by design (debuggability); operators who want pure-hash
    sharding without identifier leakage can pre-hash owner ids in
    their identity map before passing them in.
    """
    text = str(value or "default")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-").lower()[:32]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{slug or 'owner'}-{digest}"


def _load_identity_config(config: YantrikDBConfig) -> dict[str, Any]:
    """Load identity config containing actor aliases and optional groups."""
    raw: Any = None
    if config.identity_map_json:
        try:
            raw = json.loads(config.identity_map_json)
        except json.JSONDecodeError:
            logger.warning("Invalid YANTRIKDB_IDENTITY_MAP_JSON; owner aliases disabled")
            raw = None
    elif config.identity_map_path:
        try:
            raw = json.loads(Path(config.identity_map_path).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read identity map %s: %s", config.identity_map_path, e)
            raw = None
    return raw if isinstance(raw, dict) else {}


def _load_identity_map(config: YantrikDBConfig) -> dict[str, str]:
    """Load actor->owner aliases from plugin/app config.

    Supported shapes:
      {"actors": {"whatsapp:123": "owner:primary"}}
      {"owners": {"owner:primary": {"actors": ["whatsapp:123"]}}}
    """
    raw = _load_identity_config(config)
    out: dict[str, str] = {}
    actors = raw.get("actors")
    if isinstance(actors, dict):
        for actor, owner in actors.items():
            if actor and owner:
                out[str(actor)] = str(owner)

    owners = raw.get("owners")
    if isinstance(owners, dict):
        for owner, spec in owners.items():
            if isinstance(spec, dict):
                for actor in spec.get("actors") or []:
                    if actor:
                        out[str(actor)] = str(owner)
    return out


def _load_group_map(config: YantrikDBConfig) -> dict[str, dict[str, list[str]]]:
    """Load shared group/space owner config.

    Shape:
      {"groups": {"group:household": {
          "members": ["owner:primary"],
          "conversations": ["whatsapp:family-chat"]
      }}}

    The plugin only enforces this configured allow-list. Updating membership is
    an app/config operation; existing group memories stay in the group namespace.
    """
    raw = _load_identity_config(config)
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for group_id, spec in groups.items():
        if not group_id or not isinstance(spec, dict):
            continue
        members = [str(v) for v in (spec.get("members") or []) if v]
        conversations = [str(v) for v in (spec.get("conversations") or []) if v]
        out[str(group_id)] = {"members": members, "conversations": conversations}
    return out


def _configured_group_for_conversation(
    config: YantrikDBConfig,
    conversation_id: str | None,
) -> str | None:
    if not conversation_id:
        return None
    for group_id, spec in _load_group_map(config).items():
        if conversation_id in spec.get("conversations", []):
            return group_id
    return None


def _shared_groups_for_owner(config: YantrikDBConfig, owner_id: str) -> list[str]:
    groups: list[str] = []
    for group_id, spec in _load_group_map(config).items():
        if owner_id in spec.get("members", []) and group_id not in groups:
            groups.append(group_id)
    return groups

def _derive_owner_scope(config: YantrikDBConfig, kwargs: dict[str, Any]) -> dict[str, Any]:
    actor_id = _identity_actor_id(kwargs)
    conversation_id = _conversation_id(kwargs)
    aliases = _load_identity_map(config)
    actor_owner_id = aliases.get(actor_id, actor_id)
    group_owner_id = _configured_group_for_conversation(config, conversation_id)
    owner_id = group_owner_id or actor_owner_id
    owner_actors = sorted(actor for actor, owner in aliases.items() if owner == actor_owner_id)
    shared_owner_ids = [] if group_owner_id else _shared_groups_for_owner(config, actor_owner_id)
    platform = (kwargs.get("platform") or "cli").strip() or "cli"
    return {
        "owner_id": owner_id,
        "actor_owner_id": actor_owner_id,
        "actor_id": actor_id,
        "channel": platform,
        "conversation_id": conversation_id,
        "owner_actors": owner_actors,
        "shared_owner_ids": shared_owner_ids,
    }

def _derive_namespace(base: str, kwargs: dict[str, Any]) -> str:
    """Scope the namespace to ``{base}:{agent_workspace}:{agent_identity}``.

    Per HANDOFF §3 — one tenant namespace per agent identity so cross-session
    consolidation works, while preventing a second agent from polluting
    the first agent's memories.
    """
    workspace = (kwargs.get("agent_workspace") or "").strip()
    identity = (kwargs.get("agent_identity") or "").strip()
    parts = [base]
    if workspace:
        parts.append(workspace)
    if identity:
        parts.append(identity)
    return ":".join(parts)


def _format_recall_block(results: list[dict[str, Any]], limit: int = 8) -> str:
    """Markdown bullet list of recalled memories for prompt injection."""
    if not results:
        return ""
    lines: list[str] = []
    for r in results[:limit]:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        score = r.get("score")
        tag = f" _(score {score:.2f})_" if isinstance(score, (int, float)) else ""
        lines.append(f"- {text}{tag}")
    return "\n".join(lines)


def _dedupe_and_rank_results(
    result_sets: list[list[dict[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Merge recall results from scoped + shared namespaces.

    Prefer the first occurrence of the same rid/text so owner-scoped results win
    ties over base-namespace fallback results, then rank by score.
    """
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for results in result_sets:
        for r in results or []:
            key = str(r.get("rid") or r.get("text") or id(r))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)

    def _score(r: dict[str, Any]) -> float:
        raw = r.get("score")
        return float(raw) if isinstance(raw, (int, float)) else 0.0

    merged.sort(key=_score, reverse=True)
    return merged[:limit]


def _estimate_importance(text: str) -> float:
    """Cheap heuristic for ambient writes via sync_turn.

    We can't tell a greeting from a decision without an LLM call; err
    conservative. ``think()`` consolidates noise on session end.
    """
    n = len(text.strip())
    if n < 40:
        return 0.3
    if n < 200:
        return 0.5
    return 0.6


def _coerce_float(raw: Any, *, default: float) -> float:
    try:
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _parse_time_filter(value: Any) -> float | None:
    """v0.5 Wave D2 — parse a `since`/`until` value into a UNIX timestamp.

    Accepts:
      - None / empty → None (no filter)
      - ISO timestamp ("2026-05-29T00:00:00Z", "2026-05-29")
      - Relative shorthand: "today", "yesterday", "now", "last week"
      - Duration ago: "7d", "24h", "30m", "2w"
    Returns None when the value can't be parsed (caller should treat
    as "no filter applied" rather than erroring out).
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    import datetime as _dt
    now = time.time()
    today_midnight = _dt.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).timestamp()
    if s in ("now", "today"):
        return today_midnight
    if s == "yesterday":
        return today_midnight - 86400
    if s in ("last week", "lastweek", "past week", "1w"):
        return now - 7 * 86400
    if s in ("last month", "lastmonth"):
        return now - 30 * 86400
    # Duration shorthand: e.g. "7d", "24h", "30m", "2w"
    import re as _re
    m = _re.fullmatch(r"(\d+)\s*([mhdw])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}[unit]
        return now - n * mult
    # ISO date or datetime
    iso_candidates = [s, s + "T00:00:00", s.replace("z", "+00:00")]
    for iso in iso_candidates:
        try:
            dt = _dt.datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.UTC)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def _coerce_int(raw: Any, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_str_list(raw: Any) -> list[str]:
    """Coerce a tool arg into a clean list of non-empty strings.

    Accepts a list (filtered + stripped), a single string (wrapped), or
    None/anything else (empty list). Used for the recall ``reinforce`` arg.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    return []


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class YantrikDBMemoryProvider(MemoryProvider):
    """MemoryProvider implementation backed by yantrikdb-server over HTTP."""

    def __init__(self) -> None:
        self._config: YantrikDBConfig | None = None
        self._client: YantrikDBClient | None = None
        self._client_lock = threading.Lock()

        self._namespace: str = DEFAULT_NAMESPACE
        self._base_namespace: str = DEFAULT_NAMESPACE
        self._legacy_actor_namespace: str = ""
        self._shared_owner_namespaces: list[str] = []
        self._scope_metadata: dict[str, Any] = {}
        self._session_id: str = ""
        self._cron_skipped: bool = False
        # v0.4.4: when initialize() fails to construct the backend (e.g.
        # bundled-embedder download couldn't write to the engine's cache
        # dir), capture the reason here so system_prompt_block can surface
        # it to the model instead of yantrikdb appearing silently absent.
        self._init_error: str | None = None

        self._prefetch_results: dict[str, str] = {}
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: threading.Thread | None = None
        self._sync_thread: threading.Thread | None = None

        self._failure_count: int = 0
        self._breaker_open_until: float = 0.0
        self._breaker_lock = threading.Lock()

        # v0.4.17+ — path to the cross-session "recently defined skills"
        # record. Set in initialize() once hermes_home is resolved. None
        # while uninitialized (e.g. cron-skipped) or in test contexts that
        # don't pass hermes_home; the helpers no-op silently in that case.
        self._recent_skills_path: Path | None = None
        self._recent_skills_lock = threading.Lock()

        # v0.6.0+ Wave F — per-rid recall feedback ledger for self-tuning
        # recall. Same lifecycle as the skills sidecar: set in initialize()
        # once hermes_home is resolved, None otherwise (helpers no-op). Maps
        # rid -> {"surfaced": int, "reinforced": int, "last_ts": float}.
        self._recall_feedback_path: Path | None = None
        self._recall_feedback_lock = threading.Lock()

        # v0.7 Wave J — set once if the engine/server lacks the conversation
        # buffer API, so we stop attempting record_turn every turn.
        self._conversation_buffer_unavailable = False

        # v0.5.0+ Wave A — active-memory caches populated by the prefetch
        # background thread, drained by system_prompt_block().
        # A2: skill_search hits per session_id key.
        self._prefetch_skills: dict[str, list[dict[str, Any]]] = {}
        # A3: unresolved conflicts polled at most every
        # pending_conflicts_poll_seconds.
        self._pending_conflicts: list[dict[str, Any]] = []
        self._pending_conflicts_last_poll: float = 0.0

        # v0.5.0+ Wave B — the prior assistant message kept on a
        # per-session basis so that when the next user turn is a bare
        # confirmation phrase ("yes", "right", etc.), we can extract
        # from that prior assistant assertion under the HANDOFF §10.1
        # carve-out. Cleared on session switch.
        self._prior_assistant_by_session: dict[str, str] = {}

    # -- Identity ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "yantrikdb"

    # -- Setup / config ---------------------------------------------------

    def is_available(self) -> bool:
        """Ready when the configured backend can be reached. No network call.

        - embedded mode: available iff `yantrikdb` Python package is importable.
        - http mode: available iff a token is configured.
        """
        cfg = YantrikDBConfig.load()
        if cfg.mode == "embedded":
            try:
                import yantrikdb._yantrikdb_rust  # noqa: F401
                return True
            except ImportError:
                return False
        return bool(cfg.token)

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Config keys for `hermes memory setup` / `status`.

        The schema is **mode-aware** as of v0.4.3 — embedded-mode users (the
        default since v0.2.0) only see ``db_path`` + ``namespace`` as the
        relevant fields, and the legacy HTTP-only fields (``token`` / ``url``)
        are reported as optional instead of marked missing. HTTP-mode users
        get the full set with ``token`` marked required.

        The ``url`` field on each entry points at the current install docs
        in the standalone repo, not the stale ``yantrikdb.com/server/
        quickstart/`` URL that the v0.1.0 schema used (those server-CLI
        commands were renamed during the v0.7.x refactor and the docs page
        hasn't caught up yet).
        """
        mode = os.environ.get("YANTRIKDB_MODE", "embedded").strip().lower()
        readme_install = (
            "https://github.com/yantrikos/yantrikdb-hermes-plugin"
            "#install-default--embedded-backend"
        )
        readme_http = (
            "https://github.com/yantrikos/yantrikdb-hermes-plugin"
            "#install-alternative--http-backend-for-ha-cluster-setups"
        )

        # Mode selector first — makes the choice explicit in `hermes memory setup`.
        schema: list[dict[str, Any]] = [
            {
                "key": "mode",
                "description": (
                    "Backend: 'embedded' (default since v0.2.0; in-process, "
                    "~10 MB, no server) or 'http' (talks to a separately-run "
                    "yantrikdb-server, for HA cluster setups)."
                ),
                "default": "embedded",
                "env_var": "YANTRIKDB_MODE",
                "url": readme_install,
            },
        ]

        if mode == "http":
            schema.extend([
                {
                    "key": "token",
                    "description": (
                        "YantrikDB bearer token (from `yantrikdb token create` "
                        "in your running server). Required for HTTP mode only."
                    ),
                    "secret": True,
                    "required": True,
                    "env_var": "YANTRIKDB_TOKEN",
                    "url": readme_http,
                },
                {
                    "key": "url",
                    "description": "YantrikDB HTTP endpoint.",
                    "default": "http://localhost:7438",
                    "env_var": "YANTRIKDB_URL",
                },
            ])
        else:
            # embedded mode — db_path is what matters; token/url are unused.
            schema.append({
                "key": "db_path",
                "description": (
                    "SQLite path for the embedded engine. Defaults to "
                    "$HERMES_HOME/yantrikdb-memory.db when unset."
                ),
                "default": "",
                "env_var": "YANTRIKDB_DB_PATH",
                "url": readme_install,
            })

        schema.extend([
            {
                "key": "namespace",
                "description": "Tenant namespace prefix (combined with agent_workspace:agent_identity).",
                "default": "hermes",
                "env_var": "YANTRIKDB_NAMESPACE",
            },
            {
                "key": "top_k",
                "description": "Default max results for recall.",
                "default": "10",
                "env_var": "YANTRIKDB_TOP_K",
            },
            {
                "key": "owner_scoping",
                "description": (
                    "Optional Hermes gateway scoping: append a stable resolved-owner shard "
                    "to the namespace so one agent can isolate multiple users without "
                    "requiring YantrikDB core provenance columns."
                ),
                "default": "false",
                "env_var": "YANTRIKDB_OWNER_SCOPING",
            },
            {
                "key": "include_base_namespace_recall",
                "description": (
                    "When owner_scoping is enabled, also recall from the base namespace "
                    "so pre-scoping memories behave as shared/global legacy memory. "
                    "Writes still go only to the owner-scoped namespace."
                ),
                "default": "true",
                "env_var": "YANTRIKDB_INCLUDE_BASE_NAMESPACE_RECALL",
            },
            {
                "key": "include_legacy_actor_namespace_recall",
                "description": (
                    "When owner_scoping merges actors via an identity map, also recall "
                    "old per-actor owner namespaces so memories written before aliasing "
                    "remain visible to the canonical owner."
                ),
                "default": "true",
                "env_var": "YANTRIKDB_INCLUDE_LEGACY_ACTOR_NAMESPACE_RECALL",
            },
            {
                "key": "identity_map_path",
                "description": (
                    "Optional JSON file mapping platform actors to canonical owners. "
                    "Supports {'actors': {'platform:id': 'owner:id'}} or "
                    "{'owners': {'owner:id': {'actors': [...]}}}."
                ),
                "default": "",
                "env_var": "YANTRIKDB_IDENTITY_MAP_PATH",
            },
        ])
        return schema

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to $HERMES_HOME/yantrikdb.json."""
        path = Path(hermes_home) / "yantrikdb.json"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(values)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    # -- Lifecycle --------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in ("cron", "flush") or platform == "cron":
            logger.debug(
                "YantrikDB skipped: cron/flush context (agent_context=%s, platform=%s)",
                agent_context, platform,
            )
            self._cron_skipped = True
            return

        self._session_id = session_id or ""
        hermes_home_raw = kwargs.get("hermes_home")
        hermes_home = Path(hermes_home_raw) if hermes_home_raw else None
        self._config = YantrikDBConfig.load(hermes_home)
        if hermes_home is not None:
            self._recent_skills_path = hermes_home / "yantrikdb-recent-skills.json"
            self._recall_feedback_path = (
                hermes_home / "yantrikdb-recall-feedback.json"
            )

        # Embedded mode is self-contained (`pip install` and go); HTTP mode
        # requires a token. is_available() short-circuits at the provider
        # level, but be defensive here too.
        if self._config.mode == "http" and not self._config.token:
            logger.debug("YantrikDB http mode but no token — plugin inactive")
            return

        self._base_namespace = _derive_namespace(self._config.namespace, kwargs)
        self._namespace = self._base_namespace
        self._legacy_actor_namespace = ""
        self._shared_owner_namespaces = []
        self._scope_metadata = {}
        if self._config.owner_scoping:
            self._scope_metadata = _derive_owner_scope(self._config, kwargs)
            owner_shard = _safe_namespace_part(self._scope_metadata["owner_id"])
            actor_shard = _safe_namespace_part(self._scope_metadata["actor_id"])
            self._namespace = f"{self._base_namespace}:owner:{owner_shard}"
            self._legacy_actor_namespace = f"{self._base_namespace}:owner:{actor_shard}"
            self._shared_owner_namespaces = [
                f"{self._base_namespace}:owner:{_safe_namespace_part(str(owner_id))}"
                for owner_id in self._scope_metadata.get("shared_owner_ids", [])
            ]

        # v0.4.4: defensively pre-create the engine's model-cache dir before
        # any bundled-named download path runs. Otherwise an environment
        # where dirs::cache_dir() resolves to a path the engine can't create
        # (e.g. Hermes sandboxing with a custom HOME / XDG_CACHE_HOME, per
        # Issue #5) will fail set_embedder_named() and leave self._client
        # unset — and is_available() will still report True. Pre-creating
        # eliminates the trap; if it raises here, the error gets captured
        # by self._init_error and surfaced via the system prompt block
        # instead of disappearing into a WARNING log.
        if self._config.mode == "embedded":
            _ensure_engine_cache_dir()

        try:
            backend = make_backend(self._config)
        except YantrikDBError as e:
            msg = f"{self._config.mode} mode init failed: {e}"
            logger.error("YantrikDB %s", msg)
            self._init_error = msg
            return
        with self._client_lock:
            self._client = backend

        try:
            self._client.health()
            target = self._config.url if self._config.mode == "http" else (
                self._config.db_path or "default"
            )
            logger.info(
                "YantrikDB connected: mode=%s target=%s namespace=%s",
                self._config.mode, target, self._namespace,
            )
        except YantrikDBError as e:
            logger.warning(
                "YantrikDB health check failed (%s) — will retry on demand.", e,
            )

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=_SYNC_JOIN_SECS)
        with self._prefetch_lock:
            self._prefetch_results.clear()
            self._prefetch_skills.clear()
        self._pending_conflicts = []
        self._pending_conflicts_last_poll = 0.0
        self._prior_assistant_by_session.clear()
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs: Any,
    ) -> None:
        """Update cached per-session state when Hermes changes session id."""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=_PREFETCH_JOIN_SECS)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=_SYNC_JOIN_SECS)

        old_session_id = self._session_id
        self._session_id = new_session_id or ""

        # A reset/new conversation should not consume stale prefetched recall.
        # For resume/branch/compression, drop only the session we just left;
        # the new session has its own cache slot if one was queued explicitly.
        with self._prefetch_lock:
            if reset:
                self._prefetch_results.clear()
                self._prefetch_skills.clear()
            elif old_session_id:
                self._prefetch_results.pop(old_session_id, None)
                self._prefetch_skills.pop(old_session_id, None)
            if parent_session_id:
                self._prefetch_results.pop(parent_session_id, None)
                self._prefetch_skills.pop(parent_session_id, None)
        # Wave B prior-assistant buffer is session-scoped; clear by key.
        if reset:
            self._prior_assistant_by_session.clear()
        elif old_session_id:
            self._prior_assistant_by_session.pop(old_session_id, None)
            if parent_session_id:
                self._prior_assistant_by_session.pop(parent_session_id, None)
        # Conflict cache is namespace-scoped (not session-scoped), so it
        # survives session switches within the same namespace. Reset only
        # clears the timestamp so the next prefetch refreshes it.
        if reset:
            self._pending_conflicts = []
            self._pending_conflicts_last_poll = 0.0

        logger.debug(
            "YantrikDB session switched: %s -> %s (reset=%s)",
            old_session_id, self._session_id, reset,
        )

    def _require_client(self) -> YantrikDBClient:
        """Return the client or raise — keeps dispatch paths type-clean."""
        if self._client is None:
            raise RuntimeError("YantrikDB client not initialized")
        return self._client

    def _should_recall_base_namespace(self) -> bool:
        return bool(
            self._config
            and self._config.owner_scoping
            and self._config.include_base_namespace_recall
            and self._base_namespace
            and self._namespace != self._base_namespace
        )

    def _legacy_actor_namespaces(self) -> list[str]:
        if not (
            self._config
            and self._config.owner_scoping
            and self._config.include_legacy_actor_namespace_recall
            and self._base_namespace
        ):
            return []
        namespaces: list[str] = []
        for actor in self._scope_metadata.get("owner_actors") or []:
            ns = f"{self._base_namespace}:owner:{_safe_namespace_part(str(actor))}"
            if ns != self._namespace and ns not in namespaces:
                namespaces.append(ns)
        return namespaces

    def _write_scope_metadata(self) -> dict[str, Any]:
        return {
            k: v for k, v in self._scope_metadata.items()
            if k not in {"owner_actors", "shared_owner_ids"}
        }

    def _fallback_recall_namespaces(self) -> list[str]:
        namespaces: list[str] = []
        namespaces.extend(self._legacy_actor_namespaces())
        for namespace in self._shared_owner_namespaces:
            if namespace != self._namespace and namespace not in namespaces:
                namespaces.append(namespace)
        if self._should_recall_base_namespace() and self._base_namespace not in namespaces:
            namespaces.append(self._base_namespace)
        # v0.5 Wave E: union the cross-agent shared brain when opted in.
        # Sibling agents' writes appear in this agent's recall results,
        # tagged source=agent:<name> for traceability.
        shared = self._shared_brain_namespace()
        if shared and shared != self._namespace and shared not in namespaces:
            namespaces.append(shared)
        return namespaces

    def _recall_with_base_fallback(
        self,
        query: str,
        *,
        top_k: int,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        client = self._require_client()
        scoped = client.recall(
            query,
            namespace=self._namespace,
            top_k=top_k,
            domain=domain,
        ).get("results", []) or []
        fallback_sets: list[list[dict[str, Any]]] = []
        for namespace in self._fallback_recall_namespaces():
            fallback_sets.append(
                client.recall(
                    query,
                    namespace=namespace,
                    top_k=top_k,
                    domain=domain,
                ).get("results", []) or [],
            )
        if not fallback_sets:
            return scoped[:top_k]
        return _dedupe_and_rank_results([scoped, *fallback_sets], limit=top_k)

    # -- Prompt / prefetch -----------------------------------------------

    def system_prompt_block(self) -> str:
        if self._cron_skipped:
            return ""
        if self._client is None:
            # v0.4.4: surface the init failure to the model instead of
            # silently pretending memory is absent. Without this, an agent
            # whose plugin failed to initialize keeps trying to call the
            # tools and getting "not active" errors — better to tell it
            # upfront so it can adapt or alert the user.
            if self._init_error:
                return (
                    "# YantrikDB Memory — NOT AVAILABLE\n"
                    f"The plugin failed to initialize: {self._init_error}\n"
                    "Memory tools (`yantrikdb_*`) will not work this session. "
                    "Inform the user and proceed without memory tooling. "
                    "Common cause: the engine's model-cache directory "
                    "(`$XDG_CACHE_HOME/yantrikdb/models/` or "
                    "`$HOME/.cache/yantrikdb/models/`) isn't writable. "
                    "See https://github.com/yantrikos/yantrikdb-hermes-plugin/issues "
                    "for diagnostics."
                )
            return ""
        scope_line = (
            "Owner scoping enabled; memories are isolated by resolved owner namespace.\n"
            if self._scope_metadata
            else ""
        )
        base = (
            "# YantrikDB Memory\n"
            f"Active. Namespace: `{self._namespace}`.\n"
            f"{scope_line}"
            "Self-maintaining memory: canonicalizes duplicates, surfaces "
            "contradictions, ranks with recency awareness, and explains recall. "
            "Use `yantrikdb_recall` before claiming facts about the user or "
            "past decisions — each result includes a why_retrieved reason list. "
            "When a new decision or relationship surfaces, call "
            "`yantrikdb_remember` or `yantrikdb_relate`. Run `yantrikdb_think` "
            "at natural break points to consolidate duplicates and surface "
            "contradictions — then `yantrikdb_conflicts` lists what needs "
            "resolving and `yantrikdb_resolve_conflict` closes each out."
        )
        return (
            base
            + self._format_recent_skills_block()
            + self._format_auto_skill_block()
            + self._format_pending_conflicts_block()
            + self._format_hygiene_block()
            + self._format_conversation_block()
            + self._format_agenda_block()
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._cron_skipped or self._client is None:
            return ""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=_PREFETCH_JOIN_SECS)
        key = session_id or self._session_id or "__default__"
        with self._prefetch_lock:
            result = self._prefetch_results.pop(key, "")
            if not result and key != "__default__":
                result = self._prefetch_results.pop("__default__", "")
        if not result:
            return ""
        return f"## YantrikDB Recall\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._cron_skipped or self._client is None or not query:
            return
        if self._breaker_open():
            return

        # Cache key is session-scoped, which is owner-scoped in practice:
        # Hermes creates one provider instance per gateway-init, and each
        # provider's owner identity is locked at initialize() time. If
        # Hermes ever shares one provider across multiple owner identities,
        # this key would need to include the owner shard.
        key = session_id or self._session_id or "__default__"
        cfg = self._config
        client = self._client

        def _run() -> None:
            # A1: recall with min-score filter + token budget cap. Existing
            # Hermes plumbing calls prefetch() which pops this cache —
            # filtering here means low-quality hits never enter the prompt.
            try:
                results = self._recall_with_base_fallback(query, top_k=5)
                if cfg is not None:
                    results = [
                        r for r in results
                        if (r.get("score") or 0.0) >= cfg.auto_recall_min_score
                    ]
                block = _format_recall_block(results, limit=5)
                if block and cfg is not None:
                    # Rough token cap: ~4 chars per token.
                    char_cap = cfg.auto_recall_token_budget * 4
                    if len(block) > char_cap:
                        block = block[:char_cap].rsplit("\n", 1)[0] + "\n- …"
                if block:
                    with self._prefetch_lock:
                        self._prefetch_results[key] = block
                self._record_success()
            except YantrikDBClientError as e:
                logger.debug("YantrikDB prefetch rejected: %s", e)
            except YantrikDBError as e:
                self._record_failure()
                logger.debug("YantrikDB prefetch failed: %s", e)

            # A2: skill auto-attach. Same background thread piggybacks on
            # the recall round-trip — one user turn → one bg pass that
            # primes BOTH surfaces. Only stores hits at or above the
            # configured skill-match threshold.
            if cfg is not None and cfg.auto_skill_attach and cfg.skills_enabled:
                try:
                    resp = client.skill_search(
                        query, top_k=cfg.auto_skill_max_bodies * 2,
                    )
                    skills = [
                        s for s in (resp.get("skills") or [])
                        if (s.get("score") or 0.0) >= cfg.auto_skill_min_score
                    ][: cfg.auto_skill_max_bodies]
                    with self._prefetch_lock:
                        self._prefetch_skills[key] = skills
                except (YantrikDBClientError, YantrikDBError) as e:
                    logger.debug("YantrikDB skill auto-attach failed: %s", e)

            # A3: pending conflicts. Cheap call — poll at most once per
            # configured interval so per-turn cost is amortized.
            if cfg is not None and cfg.surface_pending_conflicts:
                now = time.time()
                if now - self._pending_conflicts_last_poll >= cfg.pending_conflicts_poll_seconds:
                    try:
                        resp = client.conflicts(namespace=self._namespace)
                        conflicts = resp.get("conflicts") or []
                        self._pending_conflicts = conflicts[
                            : cfg.pending_conflicts_max_surfaced
                        ]
                        self._pending_conflicts_last_poll = now
                    except (YantrikDBClientError, YantrikDBError) as e:
                        logger.debug("YantrikDB conflicts poll failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="yantrikdb-prefetch",
        )
        self._prefetch_thread.start()

    # -- Turn sync --------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist the user message + run v0.5 Wave B extraction.

        v1 behaviour preserved: when ``sync_user_messages`` is on, the
        whole user message is remembered verbatim. v0.5 Wave B adds a
        cheap-tier extraction pass over the same text that produces
        small fact candidates with ``source="extracted"`` +
        ``certainty<=0.4`` so they don't outrank canonical memories on
        default recall (filtered by ``_do_recall`` unless the caller
        opts in).

        §10.1 carve-out: when the user's message is a bare confirmation
        phrase ("yes", "right", etc.), the PRIOR assistant turn becomes
        eligible for extraction too — but ONLY then. We never extract
        from raw LLM output without explicit user assent.
        """
        if self._cron_skipped or self._client is None or self._config is None:
            return
        if self._breaker_open():
            return
        text = (user_content or "").strip()
        if not text:
            return

        cfg = self._config
        client = self._client
        snapshot_sid = self._session_id or session_id
        namespace = self._namespace

        # Determine §10.1 carve-out before clearing the prior buffer.
        from . import extractor as _ext
        prior_assistant = ""
        if cfg.extraction_enabled and _ext.is_user_confirmation(text):
            prior_assistant = self._prior_assistant_by_session.get(
                snapshot_sid, ""
            )

        def _run() -> None:
            # 1. Existing whole-message store of the user turn.
            if cfg.sync_user_messages:
                try:
                    client.remember(
                        text,
                        namespace=namespace,
                        importance=_estimate_importance(text),
                        metadata={
                            "session_id": snapshot_sid,
                            "role": "user",
                            **self._write_scope_metadata(),
                        },
                    )
                    self._record_success()
                except YantrikDBClientError as e:
                    logger.debug("YantrikDB sync_turn rejected: %s", e)
                except YantrikDBError as e:
                    self._record_failure()
                    logger.debug("YantrikDB sync_turn failed: %s", e)

            # 2. v0.5 Wave B — cheap-tier extraction.
            if cfg.extraction_enabled and cfg.extraction_tier == "cheap":
                self._extract_and_record(
                    text, speaker="user", namespace=namespace,
                    snapshot_sid=snapshot_sid,
                )
                if prior_assistant:
                    self._extract_and_record(
                        prior_assistant, speaker="assistant",
                        namespace=namespace, snapshot_sid=snapshot_sid,
                        confirmed_by_user=True,
                    )

            # 3. v0.7 Wave J — verbatim conversation buffer. Cheap, bounded,
            # survives Hermes compression so the exact last turns are always
            # recoverable via recent_turns / the optional surfaced block.
            if cfg.conversation_buffer_enabled:
                self._record_conversation_turns(
                    user_content=text,
                    assistant_content=(assistant_content or "").strip(),
                    namespace=namespace,
                )

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=_SYNC_JOIN_SECS)
        self._sync_thread = threading.Thread(
            target=_run, daemon=True, name="yantrikdb-sync",
        )
        self._sync_thread.start()

        # Track this assistant turn for the next-user-confirmation carve-out.
        ac = (assistant_content or "").strip()
        if ac:
            self._prior_assistant_by_session[snapshot_sid] = ac
        else:
            self._prior_assistant_by_session.pop(snapshot_sid, None)

    def _record_conversation_turns(
        self, *, user_content: str, assistant_content: str, namespace: str,
    ) -> None:
        """Append the user + assistant turns to the engine ring buffer.

        Fail-soft. On the first ``AttributeError`` (engine too old) or a
        404 in HTTP mode, sets ``_conversation_buffer_unavailable`` so we
        stop retrying every turn.
        """
        if self._client is None or self._config is None:
            return
        if self._conversation_buffer_unavailable:
            return
        max_turns = self._config.conversation_buffer_max_turns
        for role, content in (("user", user_content),
                              ("assistant", assistant_content)):
            if not content:
                continue
            try:
                self._client.record_turn(
                    role, content, namespace=namespace, max_turns=max_turns,
                )
            except (AttributeError, YantrikDBServerError):
                self._conversation_buffer_unavailable = True
                logger.info(
                    "YantrikDB conversation buffer unavailable "
                    "(needs yantrikdb>=0.9.0 / server endpoint) — disabling.",
                )
                return
            except YantrikDBError as e:
                logger.debug("YantrikDB record_turn failed: %s", e)
                return

    def _extract_and_record(
        self,
        text: str,
        *,
        speaker: str,
        namespace: str,
        snapshot_sid: str,
        confirmed_by_user: bool = False,
    ) -> None:
        """Run the extractor over ``text`` and write candidates."""
        if self._client is None or self._config is None:
            return
        from . import extractor as _ext
        try:
            candidates = _ext.extract_candidates(text, speaker=speaker)
        except Exception as e:
            logger.debug("YantrikDB extraction failed: %s", e)
            return
        if not candidates:
            return
        cfg = self._config
        for c in candidates:
            try:
                meta = {
                    "session_id": snapshot_sid,
                    "role": speaker,
                    "source": "extracted",
                    "extractor": c.pattern,
                    "certainty": cfg.extraction_certainty,
                    "confirmed_by_user": confirmed_by_user,
                    **c.metadata,
                    **self._write_scope_metadata(),
                }
                self._client.remember(
                    c.text,
                    namespace=namespace,
                    importance=cfg.extraction_certainty,
                    metadata=meta,
                )
            except YantrikDBClientError as e:
                logger.debug(
                    "YantrikDB extraction candidate rejected (%s): %s",
                    c.pattern, e,
                )
            except YantrikDBError as e:
                self._record_failure()
                logger.debug(
                    "YantrikDB extraction candidate write failed (%s): %s",
                    c.pattern, e,
                )

    # -- Tool dispatch ----------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        # Called by Hermes at register-time (before initialize()) to index
        # tool names → provider for routing. Must return the static schema
        # list regardless of client state; runtime readiness is enforced
        # in handle_tool_call().
        #
        # Skills are opt-in (YANTRIKDB_SKILLS_ENABLED=true). When the flag
        # is off, the three skill schemas are filtered out so the model
        # never sees them — keeping the tool surface minimal for users who
        # don't need the agentic skill loop. When `_config` is None (we
        # haven't initialized yet), expose all schemas so register-time
        # routing has every tool indexed; runtime check below short-
        # circuits if skills are disabled at call time.
        if self._cron_skipped:
            return []
        skills_enabled = bool(
            self._config.skills_enabled if self._config else
            YantrikDBConfig.load().skills_enabled
        )
        if skills_enabled:
            return list(ALL_TOOL_SCHEMAS)
        return [s for s in ALL_TOOL_SCHEMAS if not s["name"].startswith("yantrikdb_skill_")]

    def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        if self._cron_skipped or self._client is None:
            return tool_error(
                "YantrikDB is not active for this session.", tool=tool_name,
            )
        if self._breaker_open():
            return tool_error(
                "YantrikDB temporarily unavailable (circuit breaker open). "
                "Will retry automatically.",
                tool=tool_name,
            )

        try:
            raw: str | None = None
            if tool_name == "yantrikdb_remember":
                raw = self._do_remember(args)
            elif tool_name == "yantrikdb_recall":
                raw = self._do_recall(args)
            elif tool_name == "yantrikdb_forget":
                raw = self._do_forget(args)
            elif tool_name == "yantrikdb_think":
                raw = self._do_think(args)
            elif tool_name == "yantrikdb_conflicts":
                raw = self._do_conflicts()
            elif tool_name == "yantrikdb_resolve_conflict":
                raw = self._do_resolve_conflict(args)
            elif tool_name == "yantrikdb_relate":
                raw = self._do_relate(args)
            elif tool_name == "yantrikdb_stats":
                raw = self._do_stats()
            elif tool_name == "yantrikdb_pending_triggers":
                raw = self._do_pending_triggers(args)
            elif tool_name == "yantrikdb_acknowledge_trigger":
                raw = self._do_acknowledge_trigger(args)
            elif tool_name == "yantrikdb_dismiss_trigger":
                raw = self._do_dismiss_trigger(args)
            elif tool_name == "yantrikdb_act_on_trigger":
                raw = self._do_act_on_trigger(args)
            elif tool_name == "yantrikdb_extraction_stats":
                raw = self._do_extraction_stats(args)
            elif tool_name == "yantrikdb_observability":
                raw = self._do_observability(args)
            elif tool_name == "yantrikdb_hygiene":
                raw = self._do_hygiene(args)
            elif tool_name == "yantrikdb_knowledge_gaps":
                raw = self._do_knowledge_gaps(args)
            elif tool_name == "yantrikdb_recent_turns":
                raw = self._do_recent_turns(args)
            elif tool_name == "yantrikdb_tasks":
                raw = self._do_tasks(args)
            elif tool_name.startswith("yantrikdb_skill_"):
                if not (self._config and self._config.skills_enabled):
                    return tool_error(
                        "Skills are disabled. Set YANTRIKDB_SKILLS_ENABLED=true "
                        "to enable yantrikdb_skill_search / _define / _outcome.",
                        tool=tool_name,
                    )
                if tool_name == "yantrikdb_skill_search":
                    raw = self._do_skill_search(args)
                elif tool_name == "yantrikdb_skill_define":
                    raw = self._do_skill_define(args)
                elif tool_name == "yantrikdb_skill_outcome":
                    raw = self._do_skill_outcome(args)
            if raw is None:
                return tool_error(f"Unknown tool: {tool_name}", tool=tool_name)
            return _wrap_dispatch(tool_name, raw)
        except YantrikDBAuthError as e:
            self._record_failure()
            return tool_error(
                f"YantrikDB auth rejected: {e}. Check YANTRIKDB_TOKEN.",
                tool=tool_name,
            )
        except YantrikDBClientError as e:
            return tool_error(
                f"YantrikDB rejected the request: {e}", tool=tool_name,
            )
        except (YantrikDBTransientError, YantrikDBServerError) as e:
            self._record_failure()
            return tool_error(
                f"YantrikDB unavailable: {e}", tool=tool_name,
            )
        except YantrikDBError as e:
            self._record_failure()
            return tool_error(f"YantrikDB error: {e}", tool=tool_name)

    def _do_remember(self, args: dict[str, Any]) -> str:
        text = (args.get("text") or "").strip()
        if not text:
            return tool_error("Missing required parameter: text")
        importance = _coerce_float(args.get("importance"), default=0.6)
        client = self._require_client()
        resp = client.remember(
            text,
            namespace=self._namespace,
            importance=importance,
            domain=args.get("domain"),
            metadata={"session_id": self._session_id, **self._write_scope_metadata()},
        )
        # v0.5 Wave E: mirror to the cross-agent shared brain when opted in.
        # Tag with source=agent:<name> so each contributor is traceable; a
        # failed mirror write doesn't break the primary remember path.
        shared_ns = self._shared_brain_namespace()
        if shared_ns and shared_ns != self._namespace:
            try:
                client.remember(
                    text,
                    namespace=shared_ns,
                    importance=importance,
                    domain=args.get("domain"),
                    metadata={
                        "session_id": self._session_id,
                        "source": f"agent:{self._agent_name()}",
                        "shared_brain_origin_namespace": self._namespace,
                        **self._write_scope_metadata(),
                    },
                )
            except (YantrikDBClientError, YantrikDBError) as e:
                logger.debug(
                    "YantrikDB shared-brain mirror write failed (%s): %s",
                    shared_ns, e,
                )
        self._record_success()
        return json.dumps({"rid": resp.get("rid"), "stored": True})

    def _shared_brain_namespace(self) -> str:
        """Resolved shared-brain namespace; empty string when opted out."""
        if self._config is None:
            return ""
        ns = (self._config.shared_brain_namespace or "").strip()
        return ns

    def _agent_name(self) -> str:
        """Human-readable agent name for shared-brain attribution."""
        if self._config is None:
            return "unknown"
        explicit = (self._config.agent_name or "").strip()
        if explicit:
            return explicit
        # Auto-derive: prefer the second segment of agent's namespace
        # (typically agent_workspace), fall back to base_namespace, then
        # a hostname-shaped placeholder.
        parts = (self._namespace or "").split(":")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
        if self._base_namespace:
            return self._base_namespace
        return "agent"

    def _do_recall(self, args: dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return tool_error("Missing required parameter: query")
        default_top_k = self._config.top_k if self._config else 10
        top_k = min(_coerce_int(args.get("top_k"), default_top_k), 50)
        # v0.5 Wave B: candidates land with source="extracted" and
        # low certainty. Default recall hides them so they don't outrank
        # canonical memories. Caller can opt in per-call with
        # include_candidates=true OR via the config default.
        include_candidates = args.get("include_candidates")
        if include_candidates is None and self._config is not None:
            include_candidates = self._config.recall_includes_candidates
        # Pull more than top_k since we may filter some out.
        engine_top_k = top_k * 2 if not include_candidates else top_k
        results = self._recall_with_base_fallback(
            query,
            top_k=min(engine_top_k, 50),
            domain=args.get("domain"),
        )
        if not include_candidates:
            results = [
                r for r in results
                if (r.get("metadata") or {}).get("source") != "extracted"
            ]
        # v0.5 Wave D2 — time-aware since/until filter applied client-side.
        # Engine-side filter would be more efficient; this is the no-deps
        # MVP using the created_at field already returned per result.
        since_ts = _parse_time_filter(args.get("since"))
        until_ts = _parse_time_filter(args.get("until"))
        if since_ts is not None or until_ts is not None:
            kept = []
            for r in results:
                ts = r.get("created_at")
                if not isinstance(ts, (int, float)):
                    continue  # records without timestamps can't satisfy the filter
                if since_ts is not None and ts < since_ts:
                    continue
                if until_ts is not None and ts >= until_ts:
                    continue
                kept.append(r)
            results = kept

        # v0.6.0 Wave F — explicit reinforcement. The caller passes the
        # rids of memories that proved useful on a prior turn; record them
        # so future recalls rank them higher. Independent of this query's
        # results. Silent no-op when self-tuning is off or no sidecar.
        self_tuning = bool(self._config and self._config.self_tuning_recall)
        reinforce_rids = _coerce_str_list(args.get("reinforce"))
        if self_tuning and reinforce_rids:
            self._bump_recall_feedback(reinforce_rids, "reinforced")

        # v0.6.0 Wave F — self-tuning re-rank. Apply a capped boost from the
        # reinforcement ledger and re-sort BEFORE the top_k cut, so a
        # repeatedly-useful memory can climb into the returned window. Tag
        # boosted results in why_retrieved for explainability.
        boost_by_rid: dict[str, float] = {}
        if self_tuning and results:
            ledger = self._load_recall_feedback()
            for r in results:
                rid = r.get("rid")
                fb = ledger.get(rid) if rid else None
                reinforced = int((fb or {}).get("reinforced", 0))
                boost = self._recall_boost(reinforced)
                if boost > 0.0 and rid:
                    boost_by_rid[rid] = boost
            if boost_by_rid:
                results = sorted(
                    results,
                    key=lambda r: (
                        _coerce_float(r.get("score"), default=0.0)
                        + boost_by_rid.get(str(r.get("rid") or ""), 0.0)
                    ),
                    reverse=True,
                )

        results = results[:top_k]
        self._record_success()

        # v0.6.0 Wave F — record that these rids were surfaced (weak signal
        # used only by hygiene, never to boost ranking). After the top_k cut
        # so we only count what the agent actually saw.
        if self_tuning:
            surfaced_rids = [
                str(r.get("rid")) for r in results if r.get("rid")
            ]
            self._bump_recall_feedback(surfaced_rids, "surfaced")

        compact = []
        for r in results:
            why = list(r.get("why_retrieved") or [])
            rid = r.get("rid")
            if rid in boost_by_rid:
                why.append(f"reinforced (+{boost_by_rid[rid]:.2f})")
            compact.append({
                "rid": rid,
                "text": r.get("text"),
                "score": r.get("score"),
                # v0.4.17+ — full score-component breakdown from the engine.
                # Per-component values (similarity, decay, recency, importance,
                # graph_proximity, valence_multiplier) AND the weighted
                # `contributions` that sum to the final score. Makes the
                # ranking math fully visible to the agent — no opaque scores,
                # no second LLM call to "explain why." None other Hermes
                # memory provider exposes this.
                "scores": r.get("scores"),
                "importance": r.get("importance"),
                "domain": r.get("domain"),
                "created_at": r.get("created_at"),
                # Explainable recall — server returns a list of reasons per result.
                "why_retrieved": why,
            })
        return json.dumps({"count": len(compact), "results": compact})

    def _do_forget(self, args: dict[str, Any]) -> str:
        rid = (args.get("rid") or "").strip()
        if not rid:
            return tool_error("Missing required parameter: rid")
        resp = self._require_client().forget(rid)
        self._record_success()
        return json.dumps({"rid": rid, "found": bool(resp.get("found", False))})

    def _do_think(self, args: dict[str, Any]) -> str:
        resp = self._require_client().think(
            run_pattern_mining=bool(args.get("run_pattern_mining", False)),
            consolidation_limit=args.get("consolidation_limit"),
            namespace=self._namespace,
        )
        self._record_success()
        return json.dumps({
            "consolidated": resp.get("consolidation_count", 0),
            "conflicts_found": resp.get("conflicts_found", 0),
            "patterns_new": resp.get("patterns_new", 0),
            "patterns_updated": resp.get("patterns_updated", 0),
            "personality_updated": resp.get("personality_updated", False),
            "duration_ms": resp.get("duration_ms"),
            "triggers": resp.get("triggers", []),
        })

    def _do_conflicts(self) -> str:
        resp = self._require_client().conflicts(namespace=self._namespace)
        self._record_success()
        conflicts = resp.get("conflicts", []) or []
        return json.dumps({"count": len(conflicts), "conflicts": conflicts})

    def _do_resolve_conflict(self, args: dict[str, Any]) -> str:
        conflict_id = (args.get("conflict_id") or "").strip()
        strategy = (args.get("strategy") or "").strip()
        if not conflict_id or not strategy:
            return tool_error(
                "Missing required parameters: conflict_id, strategy"
            )
        if strategy == "keep_winner" and not args.get("winner_rid"):
            return tool_error("strategy='keep_winner' requires winner_rid")
        if strategy == "merge" and not args.get("new_text"):
            return tool_error("strategy='merge' requires new_text")

        resp = self._require_client().resolve_conflict(
            conflict_id,
            strategy=strategy,
            winner_rid=args.get("winner_rid"),
            new_text=args.get("new_text"),
            resolution_note=args.get("resolution_note"),
        )
        self._record_success()
        return json.dumps({
            "conflict_id": resp.get("conflict_id", conflict_id),
            "strategy": resp.get("strategy", strategy),
            "resolved": True,
        })

    def _do_relate(self, args: dict[str, Any]) -> str:
        entity = (args.get("entity") or "").strip()
        target = (args.get("target") or "").strip()
        relationship = (args.get("relationship") or "").strip()
        if not (entity and target and relationship):
            return tool_error(
                "Missing required parameters: entity, target, relationship"
            )
        resp = self._require_client().relate(
            entity, target, relationship,
            namespace=self._namespace,
        )
        self._record_success()
        return json.dumps({"edge_id": resp.get("edge_id"), "stored": True})

    def _do_stats(self) -> str:
        resp = self._require_client().stats(namespace=self._namespace)
        self._record_success()
        return json.dumps({
            "active_memories": resp.get("active_memories", 0),
            "consolidated_memories": resp.get("consolidated_memories", 0),
            "tombstoned_memories": resp.get("tombstoned_memories", 0),
            "edges": resp.get("edges", 0),
            "entities": resp.get("entities", 0),
            "operations": resp.get("operations", 0),
            "open_conflicts": resp.get("open_conflicts", 0),
            "pending_triggers": resp.get("pending_triggers", 0),
        })

    def _do_observability(self, args: dict[str, Any]) -> str:
        """v0.5 Wave C2 — one-call substrate health snapshot.

        Aggregates the four most-useful substrate signals into a single
        response so the agent (or a curious user) can answer "how is my
        memory doing" without 6 separate tool calls. Each component
        degrades gracefully — if one upstream call fails, the others
        still surface.
        """
        client = self._require_client()
        namespace = (args.get("namespace") or self._namespace or "").strip()

        snapshot: dict[str, Any] = {"namespace": namespace}

        # Engine counters
        try:
            engine_stats = client.stats(namespace=namespace)
            snapshot["engine"] = {
                "active_memories": engine_stats.get("active_memories", 0),
                "consolidated_memories": engine_stats.get(
                    "consolidated_memories", 0,
                ),
                "tombstoned_memories": engine_stats.get(
                    "tombstoned_memories", 0,
                ),
                "edges": engine_stats.get("edges", 0),
                "entities": engine_stats.get("entities", 0),
                "operations": engine_stats.get("operations", 0),
                "open_conflicts": engine_stats.get("open_conflicts", 0),
                "pending_triggers": engine_stats.get("pending_triggers", 0),
            }
        except (YantrikDBClientError, YantrikDBError) as e:
            snapshot["engine"] = {"error": str(e)}

        # Recent extraction activity (reuses extraction_stats logic)
        try:
            extraction = json.loads(
                self._do_extraction_stats({"namespace": namespace})
            )
            snapshot["extraction"] = {
                "total_sampled": extraction.get(
                    "total_candidates_sampled", 0,
                ),
                "by_pattern": extraction.get("by_pattern", {}),
                "by_speaker": extraction.get("by_speaker", {}),
            }
        except Exception as e:
            snapshot["extraction"] = {"error": str(e)}

        # Recently-defined skills (cross-session, from v0.4.17 record)
        try:
            recent = self._load_recent_skills() if hasattr(
                self, "_load_recent_skills",
            ) else []
            snapshot["recent_skills"] = [
                {
                    "skill_id": e.get("skill_id"),
                    "skill_type": e.get("skill_type"),
                    "age_seconds": int(time.time() - (e.get("ts") or time.time())),
                }
                for e in recent[-5:]
            ]
        except Exception as e:
            snapshot["recent_skills"] = {"error": str(e)}

        # Provider health: breaker + queues
        snapshot["provider"] = {
            "circuit_breaker_open": self._breaker_open(),
            "failure_count": self._failure_count,
            "prefetch_thread_alive": bool(
                self._prefetch_thread and self._prefetch_thread.is_alive()
            ),
            "sync_thread_alive": bool(
                self._sync_thread and self._sync_thread.is_alive()
            ),
        }

        # One-line human summary at the top — what an LLM should read first
        eng = snapshot["engine"] if isinstance(snapshot["engine"], dict) and "error" not in snapshot["engine"] else {}
        ext_count = (
            snapshot["extraction"].get("total_sampled", 0)
            if isinstance(snapshot["extraction"], dict)
            and "error" not in snapshot["extraction"] else 0
        )
        snapshot["summary"] = (
            f"namespace={namespace} | "
            f"memories={eng.get('active_memories', '?')} "
            f"entities={eng.get('entities', '?')} "
            f"edges={eng.get('edges', '?')} | "
            f"open_conflicts={eng.get('open_conflicts', '?')} | "
            f"extracted_candidates={ext_count} | "
            f"breaker={'OPEN' if snapshot['provider']['circuit_breaker_open'] else 'closed'}"
        )

        self._record_success()
        return json.dumps(snapshot)

    # -- v0.6.0 Wave G: proactive memory hygiene --------------------------

    # v0.7 Wave H — staleness thresholds for the engine-backed scan.
    _STALE_IMPORTANCE = 0.4          # below this is "low value"
    _STALE_AGE_SECS = 30 * 24 * 3600  # untouched for 30d is "old"
    _STALE_SCAN_CAP = 300            # max records paged per scan

    def _low_usefulness_candidates(
        self, *, min_surfaced: int = 3, limit: int = 10,
    ) -> list[dict[str, Any]]:
        """rids surfaced often in recall but never reinforced as useful.

        Plugin-side SECONDARY signal (v0.6): the self-tuning feedback ledger
        records what recall kept showing. A memory surfaced many times yet
        never reinforced is a review candidate. As of v0.7 the primary
        staleness signal is `_engine_stale_candidates` (engine truth via
        `list_records`); this remains as a complementary "shown-but-never-
        useful" overlay. Empty unless self-tuning recall populated the
        ledger. Sorted by surfaced count descending.
        """
        with self._recall_feedback_lock:
            ledger = self._load_recall_feedback()
        out: list[dict[str, Any]] = []
        for rid, fb in ledger.items():
            surfaced = int(fb.get("surfaced", 0))
            reinforced = int(fb.get("reinforced", 0))
            if reinforced == 0 and surfaced >= min_surfaced:
                out.append({
                    "rid": rid,
                    "surfaced": surfaced,
                    "reinforced": reinforced,
                    "last_ts": fb.get("last_ts"),
                })
        out.sort(key=lambda e: e["surfaced"], reverse=True)
        return out[:limit]

    def _engine_stale_candidates(
        self, namespace: str, *, limit: int = 10,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """Engine-truth stale candidates via `list_records` (v0.7 Wave H).

        Pages the namespace (bounded by `_STALE_SCAN_CAP`) and flags records
        that are low-value AND cold: `importance < _STALE_IMPORTANCE` and at
        least one of (cold storage tier, access_count <= 1, last_access older
        than `_STALE_AGE_SECS`). Returns (candidates, available, truncated).
        `available=False` when the engine/server lacks `list_records` (older
        engine or HTTP server without the endpoint) — caller falls back.
        """
        client = self._require_client()
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        truncated = False
        try:
            while len(records) < self._STALE_SCAN_CAP:
                page = client.list_records(
                    namespace=namespace,
                    limit=min(100, self._STALE_SCAN_CAP - len(records)),
                    since_rid=cursor,
                )
                batch = page.get("records") or []
                records.extend(batch)
                cursor = page.get("next_cursor")
                if not cursor or not batch:
                    break
            else:
                truncated = bool(cursor)
        except (AttributeError, YantrikDBServerError):
            # Older engine (no method) or HTTP server without /v1/records.
            return [], False, False
        except YantrikDBError:
            return [], False, False

        now = time.time()
        cands: list[dict[str, Any]] = []
        for r in records:
            imp = _coerce_float(r.get("importance"), default=0.5)
            if imp >= self._STALE_IMPORTANCE:
                continue
            ac = _coerce_int(r.get("access_count"), 0)
            la = r.get("last_access") or r.get("created_at")
            cold = (r.get("storage_tier") == "cold")
            unused = ac <= 1
            old = isinstance(la, (int, float)) and (now - la) > self._STALE_AGE_SECS
            if not (cold or unused or old):
                continue
            reasons = []
            if cold:
                reasons.append("cold")
            if unused:
                reasons.append(f"access_count={ac}")
            if old:
                reasons.append("untouched_30d")
            text = (r.get("text") or "").strip()
            cands.append({
                "rid": r.get("rid"),
                "text": text[:120] + ("…" if len(text) > 120 else ""),
                "importance": round(imp, 3),
                "access_count": ac,
                "last_access": la,
                "storage_tier": r.get("storage_tier"),
                "reason": ", ".join(reasons),
            })
        # Least valuable first.
        cands.sort(key=lambda c: (c["importance"], c["access_count"]))
        return cands[:limit], True, truncated

    def _do_hygiene(self, args: dict[str, Any]) -> str:
        """Scan for cleanup opportunities, or apply consolidate/forget.

        scan composes the existing read primitives (no engine scan API
        exists) — stats counters + open conflicts + the plugin-side
        low-usefulness signal — into one digest with a human-readable
        summary. apply runs a consolidation pass and/or forgets specific
        rids (looped, since the engine has no batch delete). Each component
        degrades gracefully.
        """
        client = self._require_client()
        namespace = (args.get("namespace") or self._namespace or "").strip()
        action = (args.get("action") or "scan").strip().lower()

        if action == "apply":
            result: dict[str, Any] = {"action": "apply", "namespace": namespace}
            if args.get("consolidate"):
                try:
                    think = client.think(
                        run_pattern_mining=False, namespace=namespace,
                    )
                    result["consolidated"] = think.get("consolidation_count", 0)
                    result["conflicts_found"] = think.get("conflicts_found", 0)
                except YantrikDBError as e:
                    result["consolidate_error"] = str(e)
            forget_rids = _coerce_str_list(args.get("forget_rids"))
            if forget_rids:
                forgotten: list[dict[str, Any]] = []
                for rid in forget_rids:
                    try:
                        resp = client.forget(rid)
                        found = bool(resp.get("found", False))
                    except YantrikDBError as e:
                        forgotten.append({"rid": rid, "error": str(e)})
                        continue
                    forgotten.append({"rid": rid, "found": found})
                    # Drop the rid from the feedback ledger too — it's gone.
                    self._purge_recall_feedback(rid)
                result["forgotten"] = forgotten
                result["forgotten_count"] = sum(
                    1 for f in forgotten if f.get("found")
                )
            if "consolidated" not in result and "forgotten" not in result:
                self._record_success()
                return tool_error(
                    "hygiene apply needs consolidate=true and/or "
                    "forget_rids=[...]. Nothing to do.",
                )
            self._record_success()
            return json.dumps(result)

        # action == "scan" (default)
        digest: dict[str, Any] = {"action": "scan", "namespace": namespace}
        try:
            engine_stats = client.stats(namespace=namespace)
            digest["engine"] = {
                "active_memories": engine_stats.get("active_memories", 0),
                "consolidated_memories": engine_stats.get(
                    "consolidated_memories", 0,
                ),
                "tombstoned_memories": engine_stats.get(
                    "tombstoned_memories", 0,
                ),
                "open_conflicts": engine_stats.get("open_conflicts", 0),
            }
        except YantrikDBError as e:
            digest["engine"] = {"error": str(e)}

        try:
            conflicts = (
                client.conflicts(namespace=namespace).get("conflicts", []) or []
            )
            cap = (
                self._config.hygiene_max_surfaced if self._config else 3
            )
            digest["open_conflicts"] = conflicts[: max(cap, 1)]
            digest["open_conflicts_total"] = len(conflicts)
        except YantrikDBError as e:
            digest["open_conflicts"] = {"error": str(e)}

        # v0.7 Wave H — primary staleness signal from engine truth, with
        # graceful fallback to the v0.6 sidecar overlay when unavailable.
        cap = self._config.hygiene_max_surfaced if self._config else 3
        stale, engine_scan, truncated = self._engine_stale_candidates(
            namespace, limit=max(cap, 1) * 3,
        )
        digest["stale_candidates"] = stale
        digest["engine_scan_available"] = engine_scan
        if truncated:
            digest["stale_scan_truncated"] = True

        low_use = self._low_usefulness_candidates()
        digest["low_usefulness"] = low_use

        eng = (
            digest["engine"]
            if isinstance(digest["engine"], dict)
            and "error" not in digest["engine"] else {}
        )
        digest["summary"] = (
            f"namespace={namespace} | "
            f"active={eng.get('active_memories', '?')} "
            f"consolidated={eng.get('consolidated_memories', '?')} "
            f"tombstoned={eng.get('tombstoned_memories', '?')} | "
            f"open_conflicts={digest.get('open_conflicts_total', '?')} | "
            f"stale={len(stale)}{'+' if truncated else ''} "
            f"low_usefulness={len(low_use)}"
        )
        digest["recommended_actions"] = self._hygiene_recommendations(
            eng, digest.get("open_conflicts_total", 0), stale, low_use,
        )
        self._record_success()
        return json.dumps(digest)

    @staticmethod
    def _hygiene_recommendations(
        eng: dict[str, Any], open_conflicts: int,
        stale: list[dict[str, Any]], low_use: list[dict[str, Any]],
    ) -> list[str]:
        recs: list[str] = []
        if open_conflicts:
            recs.append(
                f"{open_conflicts} unresolved contradiction(s) — call "
                "yantrikdb_resolve_conflict or surface to the user."
            )
        if stale:
            recs.append(
                f"{len(stale)} low-value, cold memory(ies) (low importance + "
                "rarely/never recalled) — review and forget via "
                "yantrikdb_hygiene(action=apply, forget_rids=[...])."
            )
        if low_use:
            recs.append(
                f"{len(low_use)} memory(ies) keep surfacing but were never "
                "reinforced — likely review candidates too."
            )
        if int(eng.get("active_memories", 0) or 0) > 0 and not recs:
            recs.append(
                "Substrate looks healthy. Run "
                "yantrikdb_hygiene(action=apply, consolidate=true) "
                "periodically to merge any new near-duplicates."
            )
        return recs

    def _purge_recall_feedback(self, rid: str) -> None:
        if self._recall_feedback_path is None or not rid:
            return
        with self._recall_feedback_lock:
            data = self._load_recall_feedback()
            if rid in data:
                data.pop(rid, None)
                self._save_recall_feedback(data)

    # -- v0.7 Wave I: knowledge gaps --------------------------------------

    def _do_knowledge_gaps(self, args: dict[str, Any]) -> str:
        """Surface the engine's known-unknowns. Degrades gracefully when the
        engine/server is older than 0.9.0 (no `knowledge_gaps`)."""
        client = self._require_client()
        min_count = _coerce_int(args.get("min_count"), 3)
        max_avg = _coerce_float(args.get("max_avg_top_score"), default=0.4)
        limit = _coerce_int(args.get("limit"), 20)
        namespace = (args.get("namespace") or self._namespace or "").strip()
        try:
            resp = client.knowledge_gaps(
                min_count=min_count, max_avg_top_score=max_avg, limit=limit,
                namespace=namespace or None,
            )
        except (AttributeError, YantrikDBServerError):
            return tool_error(
                "knowledge_gaps needs yantrikdb>=0.9.0 (embedded) or a "
                "yantrikdb-server exposing /v1/knowledge_gaps — not "
                "available in this mode/version.",
                tool="yantrikdb_knowledge_gaps",
            )
        gaps = resp.get("gaps") if isinstance(resp, dict) else resp
        gaps = gaps or []
        self._record_success()
        return json.dumps({
            "count": len(gaps),
            "gaps": gaps,
            "summary": (
                f"{len(gaps)} knowledge gap(s) — queries asked "
                f">={min_count}x but answered poorly "
                f"(avg top score <= {max_avg})."
            ),
        })

    # -- v0.7 Wave J: conversation buffer --------------------------------

    _CONV_UNAVAILABLE_MSG = (
        "conversation buffer needs yantrikdb>=0.9.0 (embedded) or a "
        "yantrikdb-server exposing /v1/conversation/* — not available in "
        "this mode/version."
    )

    def _do_recent_turns(self, args: dict[str, Any]) -> str:
        """Read (or clear) the verbatim conversation buffer."""
        client = self._require_client()
        namespace = (args.get("namespace") or self._namespace or "").strip()
        if args.get("clear"):
            try:
                client.clear_turns(namespace=namespace)
            except (AttributeError, YantrikDBServerError):
                return tool_error(
                    self._CONV_UNAVAILABLE_MSG, tool="yantrikdb_recent_turns",
                )
            self._record_success()
            return json.dumps({"cleared": True, "namespace": namespace})
        limit = _coerce_int(args.get("limit"), 10)
        try:
            resp = client.recent_turns(namespace=namespace, limit=limit)
        except (AttributeError, YantrikDBServerError):
            return tool_error(
                self._CONV_UNAVAILABLE_MSG, tool="yantrikdb_recent_turns",
            )
        turns = resp.get("turns") if isinstance(resp, dict) else resp
        turns = turns or []
        self._record_success()
        return json.dumps({"count": len(turns), "turns": turns})

    # -- v0.7 Wave K: task store -----------------------------------------

    _TASKS_UNAVAILABLE_MSG = (
        "tasks need yantrikdb>=0.9.0 (embedded) or a yantrikdb-server "
        "exposing /v1/tasks — not available in this mode/version."
    )

    def _do_tasks(self, args: dict[str, Any]) -> str:
        """Durable namespace-scoped task store (action-dispatched)."""
        client = self._require_client()
        action = (args.get("action") or "list").strip().lower()
        namespace = (args.get("namespace") or self._namespace or "").strip()

        def _need_id() -> str:
            return (args.get("task_id") or "").strip()

        try:
            if action == "list":
                resp = client.task_list(
                    namespace=namespace, status=args.get("status"),
                )
                tasks = (resp.get("tasks") if isinstance(resp, dict) else resp) or []
                out: dict[str, Any] = {
                    "action": "list", "count": len(tasks), "tasks": tasks,
                }
            elif action == "add":
                title = (args.get("title") or "").strip()
                if not title:
                    return tool_error(
                        "Missing required parameter: title",
                        tool="yantrikdb_tasks",
                    )
                priority = (args.get("priority") or "medium").strip().lower()
                resp = client.task_add(
                    title, namespace=namespace, priority=priority,
                    parent_id=args.get("parent_id"),
                )
                out = {"action": "add",
                       **(resp if isinstance(resp, dict) else {"id": resp})}
            elif action == "get":
                tid = _need_id()
                if not tid:
                    return tool_error(
                        "Missing required parameter: task_id",
                        tool="yantrikdb_tasks",
                    )
                out = {"action": "get", **client.task_get(tid)}
            elif action == "update":
                tid = _need_id()
                if not tid:
                    return tool_error(
                        "Missing required parameter: task_id",
                        tool="yantrikdb_tasks",
                    )
                out = {"action": "update", **client.task_update(
                    tid, status=args.get("status"),
                    priority=args.get("priority"),
                )}
            elif action == "delete":
                tid = _need_id()
                if not tid:
                    return tool_error(
                        "Missing required parameter: task_id",
                        tool="yantrikdb_tasks",
                    )
                out = {"action": "delete", **client.task_delete(tid)}
            else:
                return tool_error(
                    f"unknown action {action!r}; use "
                    "list / add / update / delete / get.",
                    tool="yantrikdb_tasks",
                )
        except (AttributeError, YantrikDBServerError):
            return tool_error(
                self._TASKS_UNAVAILABLE_MSG, tool="yantrikdb_tasks",
            )
        self._record_success()
        return json.dumps(out)

    def _do_pending_triggers(self, args: dict[str, Any]) -> str:
        limit = _coerce_int(args.get("limit"), 10)
        # Defensive bound — large limits hit the engine pointlessly when
        # the agent only needs to see what's there.
        if limit < 1:
            limit = 1
        if limit > 100:
            limit = 100
        resp = self._require_client().pending_triggers(limit=limit)
        self._record_success()
        triggers = resp.get("triggers", []) or []
        return json.dumps({"count": len(triggers), "triggers": triggers})

    def _do_acknowledge_trigger(self, args: dict[str, Any]) -> str:
        trigger_id = (args.get("trigger_id") or "").strip()
        if not trigger_id:
            return tool_error("Missing required parameter: trigger_id")
        resp = self._require_client().acknowledge_trigger(trigger_id)
        self._record_success()
        return json.dumps({
            "trigger_id": trigger_id,
            "acknowledged": bool(resp.get("acknowledged", False)),
        })

    def _do_dismiss_trigger(self, args: dict[str, Any]) -> str:
        trigger_id = (args.get("trigger_id") or "").strip()
        if not trigger_id:
            return tool_error("Missing required parameter: trigger_id")
        resp = self._require_client().dismiss_trigger(trigger_id)
        self._record_success()
        return json.dumps({
            "trigger_id": trigger_id,
            "dismissed": bool(resp.get("dismissed", False)),
        })

    def _do_act_on_trigger(self, args: dict[str, Any]) -> str:
        trigger_id = (args.get("trigger_id") or "").strip()
        if not trigger_id:
            return tool_error("Missing required parameter: trigger_id")
        resp = self._require_client().act_on_trigger(trigger_id)
        self._record_success()
        return json.dumps({
            "trigger_id": trigger_id,
            "acted": bool(resp.get("acted", False)),
        })

    def _do_skill_search(self, args: dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return tool_error("Missing required parameter: query")
        default_top_k = self._config.top_k if self._config else 10
        top_k = min(_coerce_int(args.get("top_k"), default_top_k), 50)
        resp = self._require_client().skill_search(
            query, top_k=top_k, applies_to=args.get("applies_to"),
        )
        self._record_success()
        skills = resp.get("skills") or resp.get("results") or []
        compact = [
            {
                "rid": s.get("rid"),
                "skill_id": (s.get("metadata") or {}).get("skill_id"),
                "skill_type": (s.get("metadata") or {}).get("skill_type"),
                "applies_to": (s.get("metadata") or {}).get("applies_to", []),
                "body": s.get("text"),
                "score": s.get("score"),
                "source": (s.get("metadata") or {}).get("source"),
                "why_retrieved": s.get("why_retrieved") or [],
            }
            for s in skills
        ]
        return json.dumps({"count": len(compact), "skills": compact})

    def _do_skill_define(self, args: dict[str, Any]) -> str:
        skill_id = (args.get("skill_id") or "").strip()
        body = args.get("body") or ""
        skill_type = (args.get("skill_type") or "").strip()
        applies_to = args.get("applies_to") or []
        if not skill_id or not body or not skill_type or not applies_to:
            return tool_error(
                "Missing required parameters: skill_id, body, skill_type, applies_to"
            )
        resp = self._require_client().skill_define(
            skill_id=skill_id,
            body=body,
            skill_type=skill_type,
            applies_to=applies_to,
            triggers=args.get("triggers"),
            on_conflict=args.get("on_conflict", "reject"),
            version=args.get("version"),
            supersedes_skill_id=args.get("supersedes_skill_id"),
        )
        self._record_success()
        stored = bool(resp.get("stored", True))
        # v0.4.17+ visible auto-skill crystallization. Only record on
        # actual store; on-conflict rejects (stored=False) are NOT new
        # learning and shouldn't trigger next-session notification.
        if stored:
            self._record_recent_skill(
                skill_id=resp.get("skill_id", skill_id),
                skill_type=skill_type,
                applies_to=applies_to,
            )
            logger.info(
                "YantrikDB skill defined: %s (%s) — will surface in next session prompt",
                skill_id, skill_type,
            )
        return json.dumps({
            "rid": resp.get("rid"),
            "skill_id": resp.get("skill_id", skill_id),
            "stored": stored,
        })

    # -- v0.4.17 visible auto-skill crystallization ----------------------
    #
    # Skills the agent defines via `yantrikdb_skill_define` are full-fledged
    # learning artifacts. Pre-v0.4.17 the model could write one, the session
    # would end, and no future session ever knew it existed unless something
    # happened to call `skill_search` with the right query. The wow is closing
    # that loop: persist a small (skill_id, type, ts) trail across sessions,
    # then surface "the agent learned these recently" in the next session's
    # system prompt. The incoming model sees its own prior learning the
    # moment it boots.
    #
    # Persistence is a JSON file under hermes_home (same dir as the config).
    # Bounded at 10 entries; only entries ≤7d old surface; the current
    # session's own entries are filtered out (the agent already knows about
    # them — surfacing them would just be noise). Failures during read/write
    # are swallowed: this is a UX nicety, not load-bearing.

    _RECENT_SKILLS_MAX = 10
    _RECENT_SKILLS_TTL_SECS = 7 * 24 * 3600

    def _record_recent_skill(
        self,
        *,
        skill_id: str,
        skill_type: str,
        applies_to: Any,
    ) -> None:
        if self._recent_skills_path is None:
            return
        entry = {
            "skill_id": skill_id,
            "skill_type": skill_type,
            "applies_to": applies_to if isinstance(applies_to, list) else [],
            "ts": time.time(),
            "session_id": self._session_id,
        }
        with self._recent_skills_lock:
            try:
                entries = self._load_recent_skills()
                # Drop any prior entry with the same skill_id — the latest
                # write is the authoritative one (e.g. supersedes or replace
                # via on_conflict=replace would otherwise show two).
                entries = [e for e in entries if e.get("skill_id") != skill_id]
                entries.append(entry)
                entries = entries[-self._RECENT_SKILLS_MAX:]
                self._recent_skills_path.parent.mkdir(parents=True, exist_ok=True)
                self._recent_skills_path.write_text(
                    json.dumps(entries, indent=2), encoding="utf-8",
                )
            except OSError as e:
                logger.debug("recent-skills persist failed: %s", e)

    def _load_recent_skills(self) -> list[dict[str, Any]]:
        if self._recent_skills_path is None or not self._recent_skills_path.exists():
            return []
        try:
            raw = json.loads(self._recent_skills_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("recent-skills read failed: %s", e)
            return []
        return raw if isinstance(raw, list) else []

    def _format_recent_skills_block(self) -> str:
        if self._config is None or not self._config.surface_recent_skills:
            return ""
        with self._recent_skills_lock:
            entries = self._load_recent_skills()
        if not entries:
            return ""
        now = time.time()
        cutoff = now - self._RECENT_SKILLS_TTL_SECS
        # Surface entries from PRIOR sessions only — current session
        # already knows what it just defined.
        fresh = [
            e for e in entries
            if e.get("ts", 0) >= cutoff
            and e.get("session_id") != self._session_id
        ]
        if not fresh:
            return ""
        lines = ["", "## Recently learned skills"]
        for e in fresh[-5:]:  # cap the prompt budget
            age_h = int((now - e.get("ts", now)) / 3600)
            age_str = f"{age_h}h ago" if age_h < 48 else f"{age_h // 24}d ago"
            applies = e.get("applies_to") or []
            scope = (
                f" scope={','.join(map(str, applies[:3]))}"
                if applies else ""
            )
            lines.append(
                f"- `{e.get('skill_id', '?')}` ({e.get('skill_type', '?')}){scope} — {age_str}"
            )
        lines.append(
            "The agent defined these in prior sessions. If your task "
            "matches any, call `yantrikdb_skill_search` to retrieve the body."
        )
        return "\n".join(lines)

    # -- v0.6.0 Wave F: recall feedback ledger (self-tuning recall) --------
    #
    # Local per-rid usefulness ledger. ``surfaced`` counts how often a
    # memory appeared in recall results; ``reinforced`` counts explicit
    # "this proved useful" signals (recall(reinforce=[rid])). Only
    # reinforcement boosts ranking — surfaced-only frequency would entrench
    # whatever already ranks high. Capped + pruned by recency so the file
    # stays small. Fail-soft: any IO error degrades to "no feedback."

    _RECALL_FEEDBACK_MAX = 1000

    def _load_recall_feedback(self) -> dict[str, dict[str, Any]]:
        if (
            self._recall_feedback_path is None
            or not self._recall_feedback_path.exists()
        ):
            return {}
        try:
            raw = json.loads(
                self._recall_feedback_path.read_text(encoding="utf-8"),
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("recall-feedback read failed: %s", e)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_recall_feedback(self, data: dict[str, dict[str, Any]]) -> None:
        if self._recall_feedback_path is None:
            return
        # Prune to the most-recently-touched entries if oversized.
        if len(data) > self._RECALL_FEEDBACK_MAX:
            ordered = sorted(
                data.items(),
                key=lambda kv: kv[1].get("last_ts", 0.0),
                reverse=True,
            )
            data = dict(ordered[: self._RECALL_FEEDBACK_MAX])
        try:
            self._recall_feedback_path.parent.mkdir(parents=True, exist_ok=True)
            self._recall_feedback_path.write_text(
                json.dumps(data), encoding="utf-8",
            )
        except OSError as e:
            logger.debug("recall-feedback persist failed: %s", e)

    def _bump_recall_feedback(self, rids: list[str], field: str) -> None:
        """Increment ``field`` ('surfaced' or 'reinforced') for each rid."""
        if self._recall_feedback_path is None or not rids:
            return
        now = time.time()
        with self._recall_feedback_lock:
            data = self._load_recall_feedback()
            for rid in rids:
                if not rid:
                    continue
                entry = data.get(rid) or {"surfaced": 0, "reinforced": 0}
                entry[field] = int(entry.get(field, 0)) + 1
                entry["last_ts"] = now
                data[rid] = entry
            self._save_recall_feedback(data)

    def _recall_boost(self, reinforced: int) -> float:
        """Capped score boost from explicit reinforcement count.

        Linear in reinforcement up to ``self_tuning_max_boost``. ~3
        reinforcements reach the cap at the default 0.15 / 0.05 settings.
        """
        if reinforced <= 0:
            return 0.0
        max_boost = (
            self._config.self_tuning_max_boost if self._config else 0.15
        )
        return min(max_boost, 0.05 * reinforced)

    # -- v0.5.0 Wave B: extraction stats handler --------------------------

    def _do_extraction_stats(self, args: dict[str, Any]) -> str:
        """Surface per-extractor counts of candidate facts.

        MVP: probe the substrate via recall on broad queries, post-filter
        to ``source="extracted"``, group by ``metadata.extractor``. Not
        a perfect count (recall is similarity-bounded), but gives a
        useful tuning signal. A future engine-side ``list_by_metadata``
        API would let us return exact counts.
        """
        client = self._require_client()
        namespace = (args.get("namespace") or self._namespace or "").strip()
        # Probe with several broad queries to widen coverage; dedupe by rid.
        probes = ["user", "agent", "is", "prefers", "name"]
        seen: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for q in probes:
            try:
                resp = client.recall(query=q, top_k=50, namespace=namespace)
            except (YantrikDBClientError, YantrikDBError):
                continue
            for r in (resp.get("results") or []):
                rid = r.get("rid") or r.get("id")
                if not rid or rid in seen:
                    continue
                meta = r.get("metadata") or {}
                if meta.get("source") != "extracted":
                    continue
                seen.add(rid)
                candidates.append(r)

        by_pattern: dict[str, int] = {}
        by_speaker: dict[str, int] = {}
        for c in candidates:
            meta = c.get("metadata") or {}
            pattern = meta.get("extractor") or "unknown"
            by_pattern[pattern] = by_pattern.get(pattern, 0) + 1
            speaker = meta.get("speaker") or meta.get("role") or "unknown"
            by_speaker[speaker] = by_speaker.get(speaker, 0) + 1

        out = {
            "namespace": namespace,
            "total_candidates_sampled": len(candidates),
            "by_pattern": dict(sorted(by_pattern.items(), key=lambda kv: -kv[1])),
            "by_speaker": by_speaker,
            "sampling_method": "probe_recall",
            "note": (
                "Counts are recall-bounded estimates, not exact. "
                "Use to identify noisy patterns (high count + you rarely "
                "see them confirmed in your own usage) and disable them "
                "via stricter regex or YANTRIKDB_EXTRACTION_ENABLED=0."
            ),
        }
        return json.dumps(out)

    # -- v0.5.0 Wave A2: skill auto-attach surface ------------------------

    def _format_auto_skill_block(self) -> str:
        """Surface skill_search hits the prefetch thread cached for this turn.

        Drain-on-read (pop) so the same hit doesn't echo across consecutive
        turns. Per Wave A semantics: a skill auto-surfaces on the turn its
        relevance was detected; subsequent turns get fresh hits or none.
        """
        if self._config is None or not self._config.auto_skill_attach:
            return ""
        key = self._session_id or "__default__"
        with self._prefetch_lock:
            skills = self._prefetch_skills.pop(key, [])
            if not skills and key != "__default__":
                skills = self._prefetch_skills.pop("__default__", [])
        if not skills:
            return ""
        lines = ["", "## Active skill — auto-surfaced for this turn"]
        for s in skills:
            # HTTP backend returns flat keys; embedded returns the skill
            # body as `text` with metadata nested under `metadata.*`.
            # Normalize to support either shape transparently.
            meta = s.get("metadata") or {}
            sid = s.get("skill_id") or meta.get("skill_id") or "?"
            stype = s.get("skill_type") or meta.get("skill_type") or "?"
            body = (s.get("body") or s.get("text") or "").strip()
            score = s.get("score")
            score_tag = (
                f" _(match {score:.2f})_"
                if isinstance(score, (int, float)) else ""
            )
            lines.append(f"- `{sid}` ({stype}){score_tag}")
            if body:
                # One-line body fold so dense skills don't blow the prompt.
                if len(body) > 280:
                    body = body[:278] + "…"
                lines.append(f"    {body}")
        lines.append(
            "Surfaced because your message matched this skill semantically. "
            "Apply if relevant; otherwise ignore and proceed."
        )
        return "\n".join(lines)

    # -- v0.5.0 Wave A3: pending-conflict surface -------------------------

    def _format_pending_conflicts_block(self) -> str:
        """Show open conflicts() entries the agent should know about.

        Read-only peek of the cached poll — does NOT clear, because a
        conflict remains live until resolve_conflict() lands. Repeat
        surfacing across turns is intentional: an unresolved contradiction
        is a standing piece of context.
        """
        if self._config is None or not self._config.surface_pending_conflicts:
            return ""
        conflicts = self._pending_conflicts
        if not conflicts:
            return ""
        lines = ["", "## Pending contradictions in your memory"]
        for c in conflicts[: self._config.pending_conflicts_max_surfaced]:
            cid = c.get("conflict_id") or c.get("rid") or "?"
            a = (c.get("text_a") or c.get("a") or "").strip()
            b = (c.get("text_b") or c.get("b") or "").strip()
            lines.append(f"- `{cid}`")
            if a:
                lines.append(f"    A: {a[:160]}{'…' if len(a) > 160 else ''}")
            if b:
                lines.append(f"    B: {b[:160]}{'…' if len(b) > 160 else ''}")
        lines.append(
            "Unresolved. Call `yantrikdb_resolve_conflict` when a decision "
            "is made, or surface to the user if you need their input."
        )
        return "\n".join(lines)

    def _format_hygiene_block(self) -> str:
        """v0.6 Wave G — passively surface memory-hygiene opportunities.

        Opt-in (YANTRIKDB_SURFACE_HYGIENE=true). Shows the plugin-side
        low-usefulness candidates (memories that keep surfacing in recall
        but were never reinforced) so the agent can proactively clean up
        without being asked. Cheap: reads only the local feedback ledger,
        no engine round-trip. Empty unless self-tuning recall has populated
        the ledger.
        """
        if self._config is None or not self._config.surface_hygiene:
            return ""
        cap = max(self._config.hygiene_max_surfaced, 1)
        low_use = self._low_usefulness_candidates(limit=cap)
        if not low_use:
            return ""
        lines = ["", "## Memory hygiene — review candidates"]
        for e in low_use:
            lines.append(
                f"- `{e['rid']}` surfaced {e['surfaced']}× in recall, "
                "never reinforced as useful."
            )
        lines.append(
            "These keep appearing without proving useful. Call "
            "`yantrikdb_hygiene` to inspect, then forget the stale ones."
        )
        return "\n".join(lines)

    def _format_conversation_block(self) -> str:
        """v0.7 Wave J — optionally surface the verbatim recent turns.

        Opt-in (YANTRIKDB_SURFACE_CONVERSATION_BUFFER=true). Most useful
        post-compression, when the semantic store has the gist but the exact
        last turns would otherwise be gone. Costs one engine read per call
        (hence opt-in). Fail-soft; disables itself if the buffer API is
        absent.
        """
        if self._config is None or not self._config.surface_conversation_buffer:
            return ""
        if self._client is None or self._conversation_buffer_unavailable:
            return ""
        limit = max(self._config.conversation_buffer_surface_limit, 1)
        try:
            resp = self._client.recent_turns(
                namespace=self._namespace, limit=limit,
            )
        except (AttributeError, YantrikDBServerError):
            self._conversation_buffer_unavailable = True
            return ""
        except YantrikDBError:
            return ""
        turns = (resp.get("turns") if isinstance(resp, dict) else resp) or []
        if not turns:
            return ""
        lines = ["", "## Recent conversation (verbatim)"]
        for t in turns[-limit:]:
            role = t.get("role", "?")
            content = (t.get("content") or "").strip()
            snippet = content[:200] + ("…" if len(content) > 200 else "")
            lines.append(f"- **{role}**: {snippet}")
        lines.append("(Preserved verbatim by YantrikDB across compression.)")
        return "\n".join(lines)

    def _format_agenda_block(self) -> str:
        """v0.8 — the self-directing substrate's agenda.

        Opt-in (YANTRIKDB_SURFACE_AGENDA=true). Prepends what the memory
        still needs: open tasks (agent-authored + auto-created from knowledge
        gaps) and the top unresolved knowledge gaps. Turns the substrate from
        a passive store into an active participant that hands the agent its
        own to-do list. Fail-soft; empty when nothing is pending or the APIs
        are unavailable.
        """
        if self._config is None or not self._config.surface_agenda:
            return ""
        if self._client is None:
            return ""
        cap = max(self._config.agenda_max_items, 1)
        tasks: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        try:
            listed = self._client.task_list(
                namespace=self._namespace, status="open",
            )
            tasks = (listed.get("tasks") if isinstance(listed, dict) else listed) or []
        except (AttributeError, YantrikDBServerError, YantrikDBError):
            tasks = []
        try:
            resp = self._client.knowledge_gaps(
                max_avg_top_score=self._config.gap_max_avg_top_score, limit=cap,
                namespace=self._namespace,
            )
            gaps = (resp.get("gaps") if isinstance(resp, dict) else resp) or []
        except (AttributeError, YantrikDBServerError, YantrikDBError):
            gaps = []
        if not tasks and not gaps:
            return ""
        lines = ["", "## Your memory's agenda"]
        if tasks:
            lines.append("Open tasks:")
            for t in tasks[:cap]:
                title = (t.get("title") or "").strip()
                pri = t.get("priority") or "medium"
                lines.append(f"- [{pri}] {title}  (`{t.get('id', '?')}`)")
        if gaps:
            lines.append("Unanswered (asked often, answered poorly):")
            for g in gaps[:cap]:
                q = (g.get("query") if isinstance(g, dict) else str(g)) or ""
                lines.append(f"- {q.strip()}")
        lines.append(
            "Address a gap, then mark its task done with "
            "`yantrikdb_tasks(action=update, status=done)`."
        )
        return "\n".join(lines)

    def _do_skill_outcome(self, args: dict[str, Any]) -> str:
        skill_id = (args.get("skill_id") or "").strip()
        if not skill_id:
            return tool_error("Missing required parameter: skill_id")
        if "succeeded" not in args:
            return tool_error("Missing required parameter: succeeded")
        resp = self._require_client().skill_outcome(
            skill_id,
            bool(args.get("succeeded")),
            note=args.get("note"),
        )
        self._record_success()
        return json.dumps({
            "rid": resp.get("rid"),
            "skill_id": resp.get("skill_id", skill_id),
            "recorded": bool(resp.get("recorded", True)),
        })

    # -- Optional hooks ---------------------------------------------------

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Run consolidation at session end, flushing pending writes first."""
        if self._cron_skipped or self._client is None or self._config is None:
            return
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=_SESSION_END_JOIN_SECS)
        if not self._config.auto_think_on_session_end:
            return
        if self._breaker_open():
            return
        try:
            stats = self._client.think(
                run_pattern_mining=False,
                run_personality=False,
                namespace=self._namespace,
            )
            logger.info(
                "YantrikDB session-end think: consolidated=%s conflicts=%s duration_ms=%s",
                stats.get("consolidation_count"),
                stats.get("conflicts_found"),
                stats.get("duration_ms"),
            )
        except YantrikDBError as e:
            logger.debug("YantrikDB session-end think failed: %s", e)
            return

        # v0.4.15+ — drain the pending-trigger queue when configured.
        # Without this, triggers think() produces accumulate across
        # sessions for users who don't implement a consumer loop
        # (issue #22). Conservative: `acknowledge` not `act_on` /
        # `dismiss`, since no action was actually taken. Fail-soft —
        # one bad trigger doesn't block the rest.
        if self._config.auto_acknowledge_triggers:
            self._auto_acknowledge_pending_triggers()

        # v0.8 — self-directing substrate: turn recurring knowledge gaps into
        # durable tasks so the agent's unanswered questions become an agenda.
        if self._config.auto_gap_tasks:
            self._auto_gap_tasks()

    def _auto_gap_tasks(self) -> None:
        """Convert recurring knowledge gaps into durable tasks (v0.8).

        Runs ``knowledge_gaps()`` and, for each gap not already covered by an
        open task, creates ``Resolve knowledge gap: <query>``. Bounded per
        session (``gap_task_max``); dedups against existing task titles.
        Fail-soft; silently disables on engines/servers without the APIs.
        """
        if self._client is None or self._config is None:
            return
        cfg = self._config
        try:
            resp = self._client.knowledge_gaps(
                min_count=cfg.gap_task_min_count,
                max_avg_top_score=cfg.gap_max_avg_top_score,
                limit=max(cfg.gap_task_max * 3, 1),
                namespace=self._namespace,
            )
            gaps = (resp.get("gaps") if isinstance(resp, dict) else resp) or []
        except (AttributeError, YantrikDBServerError):
            return
        except YantrikDBError as e:
            logger.debug("YantrikDB auto-gap-tasks knowledge_gaps failed: %s", e)
            return
        if not gaps:
            return
        try:
            listed = self._client.task_list(namespace=self._namespace)
            tasks = (listed.get("tasks") if isinstance(listed, dict) else listed) or []
            existing = {(t.get("title") or "").strip().lower() for t in tasks}
        except (AttributeError, YantrikDBServerError):
            return
        except YantrikDBError:
            existing = set()
        created = 0
        for g in gaps:
            if created >= cfg.gap_task_max:
                break
            query = (g.get("query") if isinstance(g, dict) else str(g)) or ""
            query = query.strip()
            if not query:
                continue
            title = f"Resolve knowledge gap: {query}"
            if title.lower() in existing:
                continue
            try:
                self._client.task_add(
                    title, namespace=self._namespace, priority="medium",
                )
                existing.add(title.lower())
                created += 1
            except YantrikDBError as e:
                logger.debug("YantrikDB auto-gap-task create failed: %s", e)
        if created:
            logger.info(
                "YantrikDB self-directing: created %d gap task(s) at session end",
                created,
            )

    def _auto_acknowledge_pending_triggers(self) -> None:
        """Drain the pending-trigger queue by calling acknowledge on each.

        Loops until the queue is empty (or the safety cap of 10 batches
        fires). Each batch pulls 50 triggers; that gives 500-trigger
        headroom per session — well above any realistic load — without
        unbounded teardown time. Logs at debug on per-trigger errors so
        one corrupted trigger doesn't stop the batch.

        HTTP mode caveat: yantrikdb-server hasn't shipped the
        ``/v1/triggers/*`` endpoints yet (tracked upstream). When the
        first call 404s, we log a single WARNING and bail — silent
        no-op would let users believe auto-ack is working when it
        isn't.
        """
        if self._client is None:
            return
        BATCH = 50
        MAX_BATCHES = 10  # 500-trigger ceiling per session
        total_seen = 0
        total_acked = 0
        for _batch_idx in range(MAX_BATCHES):
            try:
                resp = self._client.pending_triggers(limit=BATCH)
            except YantrikDBServerError as e:
                # HTTP-mode 404: server doesn't ship the trigger
                # endpoints yet. Loud once so the user knows
                # auto-ack is effectively off in this mode.
                if "501" in str(e) or "issues/39" in str(e) or "needs yantrikdb-server" in str(e):
                    logger.warning(
                        "YantrikDB auto-acknowledge unavailable in HTTP "
                        "mode — yantrikdb-server has not yet shipped the "
                        "/v1/triggers/* endpoints. Tracking upstream. "
                        "Set YANTRIKDB_AUTO_ACKNOWLEDGE_TRIGGERS=false to "
                        "silence this; the engine's 7-day TTL still bounds "
                        "trigger accumulation."
                    )
                else:
                    logger.debug(
                        "YantrikDB auto-acknowledge list-pending failed: %s", e,
                    )
                return
            except YantrikDBError as e:
                logger.debug("YantrikDB auto-acknowledge list-pending failed: %s", e)
                return
            triggers = resp.get("triggers", []) or []
            if not triggers:
                break
            total_seen += len(triggers)
            for t in triggers:
                tid = (t.get("trigger_id") or t.get("id") or "").strip()
                if not tid:
                    continue
                try:
                    self._client.acknowledge_trigger(tid)
                    total_acked += 1
                except YantrikDBError as e:
                    logger.debug(
                        "YantrikDB auto-acknowledge failed for trigger %s: %s",
                        tid, e,
                    )
            # If this batch was short, the queue is drained — exit
            # without the extra round-trip.
            if len(triggers) < BATCH:
                break
        else:
            # else on for: ran MAX_BATCHES rounds without a short batch.
            # Surface this — sustained high trigger production may be
            # a signal the user should investigate.
            logger.warning(
                "YantrikDB auto-acknowledge stopped at safety cap of "
                "%d batches (%d triggers). More may remain pending; the "
                "engine's 7-day TTL will eventually clean them up.",
                MAX_BATCHES, total_seen,
            )
        if total_seen:
            logger.info(
                "YantrikDB auto-acknowledged %d/%d pending triggers at session end",
                total_acked, total_seen,
            )

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Preserve high-salience context across Hermes compression.

        Pre-v0.5 behaviour preserved: seeds recall with the tail of the
        about-to-be-compressed messages and returns a markdown block that
        Hermes' compressor includes in the summary prompt.

        v0.5 Wave D1 additions: also snapshots a one-line gist of the
        being-dropped middle (skipping the tail Hermes preserves
        verbatim) into the substrate as a high-importance memory tagged
        `pre_compression=true`. Post-compression recall can resurface
        it via `yantrikdb_recall` like any other memory; the
        `pre_compression` tag lets the agent or stats tool distinguish
        compression-summary memories from ordinary records.
        """
        if self._cron_skipped or self._client is None:
            return ""
        if self._breaker_open():
            return ""
        if not messages:
            return ""

        tail = " ".join(
            (m.get("content") or "") for m in messages[-6:] if isinstance(m, dict)
        ).strip()

        # v0.5 D1: snapshot the gist of the dropped middle.
        # `messages` is what Hermes is ABOUT TO compress; we don't know
        # which segment survives Hermes' tail-keep heuristic, but anything
        # we record here will survive compression because it's in the
        # substrate, not the conversation buffer.
        middle = [
            m for m in messages[:-6]
            if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
        ]
        if middle:
            gist = self._compose_compression_gist(middle)
            if gist:
                try:
                    self._client.remember(
                        gist,
                        namespace=self._namespace,
                        importance=0.75,  # higher than baseline; this is a deliberate save
                        metadata={
                            "session_id": self._session_id,
                            "source": "compression_summary",
                            "pre_compression": True,
                            "turns_summarized": len(middle),
                            **self._write_scope_metadata(),
                        },
                    )
                    self._record_success()
                except (YantrikDBClientError, YantrikDBError) as e:
                    logger.debug(
                        "YantrikDB on_pre_compress gist write failed: %s", e,
                    )

        if not tail:
            return ""
        try:
            resp = self._client.recall(
                tail[:2000], namespace=self._namespace, top_k=8,
            )
            block = _format_recall_block(resp.get("results", []), limit=8)
            if not block:
                return ""
            return f"## YantrikDB memories to preserve\n{block}"
        except YantrikDBError as e:
            logger.debug("YantrikDB on_pre_compress failed: %s", e)
            return ""

    def _compose_compression_gist(self, middle: list[dict[str, Any]]) -> str:
        """Distill the middle of a conversation into a single-line gist.

        v0.5 D1 MVP: take the first user turn (intent) + the count of
        message exchanges + the last assistant turn (outcome) as a
        machine-readable summary. Engine-side LLM summarization would
        be more semantic; this preserves the discoverable shape with
        zero new dependencies.
        """
        first_user = next(
            (m.get("content", "") for m in middle if m.get("role") == "user"),
            "",
        )
        last_assistant = next(
            (m.get("content", "") for m in reversed(middle)
             if m.get("role") == "assistant"),
            "",
        )
        if not first_user:
            return ""
        intent = (first_user or "").strip().replace("\n", " ")[:200]
        outcome = (last_assistant or "").strip().replace("\n", " ")[:200]
        n = len(middle)
        if outcome:
            return (
                f"[compression-summary, {n} turns] "
                f"intent: {intent} | outcome: {outcome}"
            )
        return f"[compression-summary, {n} turns] intent: {intent}"

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        """Mirror built-in MEMORY.md / USER.md additions into YantrikDB."""
        if self._cron_skipped or self._client is None:
            return
        if action != "add" or target not in ("memory", "user") or not content:
            return
        if self._breaker_open():
            return

        client = self._client
        text = content
        session_id = self._session_id
        namespace = self._namespace
        domain = "user" if target == "user" else "work"

        def _run() -> None:
            try:
                client.remember(
                    text,
                    namespace=namespace,
                    importance=0.7,
                    domain=domain,
                    metadata={
                        "source": "hermes_memory_md",
                        "target": target,
                        "session_id": session_id,
                        **self._write_scope_metadata(),
                    },
                )
                self._record_success()
            except YantrikDBError as e:
                self._record_failure()
                logger.debug("YantrikDB on_memory_write failed: %s", e)

        threading.Thread(
            target=_run, daemon=True, name="yantrikdb-memwrite",
        ).start()

    # -- Circuit breaker --------------------------------------------------

    def _breaker_open(self) -> bool:
        with self._breaker_lock:
            if self._failure_count < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._failure_count = 0
                return False
            return True

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._failure_count = 0

    def _record_failure(self) -> None:
        with self._breaker_lock:
            self._failure_count += 1
            if self._failure_count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN
                logger.warning(
                    "YantrikDB circuit breaker tripped after %d failures — "
                    "pausing for %ds.",
                    self._failure_count, _BREAKER_COOLDOWN,
                )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    """Register YantrikDB as a memory provider plugin."""
    ctx.register_memory_provider(YantrikDBMemoryProvider())


__all__ = [
    "ACKNOWLEDGE_TRIGGER_SCHEMA",
    "ACT_ON_TRIGGER_SCHEMA",
    "ALL_TOOL_SCHEMAS",
    "CONFLICTS_SCHEMA",
    "DISMISS_TRIGGER_SCHEMA",
    "FORGET_SCHEMA",
    "PENDING_TRIGGERS_SCHEMA",
    "RECALL_SCHEMA",
    "RELATE_SCHEMA",
    "REMEMBER_SCHEMA",
    "RESOLVE_CONFLICT_SCHEMA",
    "STATS_SCHEMA",
    "THINK_SCHEMA",
    "YantrikDBMemoryProvider",
    "register",
]
