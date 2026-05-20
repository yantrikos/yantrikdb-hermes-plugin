"""Seed data for the LLM-driven demo — representative skills shaped like
real production entries from a long-running yantrikdb skill_substrate.

These are anonymized/genericized versions of the kinds of patterns an
agent actually crystallizes over many sessions: incident lessons,
operational procedures, research protocols, debugging references.

Loaded into the ephemeral demo substrate before the user prompt in
session 1, so the demo's "skills before -> skills after" delta reads
as "agent adds to a lived-in substrate," not "agent populates a toy."
"""
from __future__ import annotations

from typing import Any

SEED_SKILLS: list[dict[str, Any]] = [
    {
        "skill_id": "research.preregistration.protocol",
        "skill_type": "procedure",
        "applies_to": ["research", "methodology", "review"],
        "body": (
            "For any research attempt in a single session: "
            "(1) pre-register the hypothesis and falsification criteria "
            "in writing BEFORE looking at data; "
            "(2) define the minimum test that could falsify the claim; "
            "(3) declare what data you will collect and how it will be analyzed; "
            "(4) commit the pre-registration to the substrate before running anything. "
            "If you can't pre-register, the result is exploratory, not confirmatory."
        ),
        "triggers": ["new experiment", "test hypothesis", "research session"],
    },
    {
        "skill_id": "deploy.allowed_kinds.extension_order",
        "skill_type": "lesson",
        "applies_to": ["deployment", "incident", "review"],
        "body": (
            "When extending an ALLOWED_KINDS list (or any similar allow-list) that's "
            "checked in BOTH a polling watcher AND a downstream ingest service, the "
            "deploy ORDER matters: ingest must accept the new kind FIRST, then the "
            "watcher starts emitting it. If you deploy the watcher first, the ingest "
            "rejects the new kind during the deploy gap and you lose those events "
            "silently. Always: downstream first, upstream second."
        ),
        "triggers": ["extend allowed kinds", "add new event type", "deploy ordering"],
    },
    {
        "skill_id": "incident.service.silent_deadlock_check",
        "skill_type": "reference",
        "applies_to": ["incident", "debugging", "operations"],
        "body": (
            "Symptom shape — a long-running service has /v1/health returning ok "
            "but /v1/<operation> hanging indefinitely on otherwise-valid requests. "
            "Likely causes ranked: (1) blocked on a mutex held by a panicked thread "
            "(check thread state with py-spy or gdb); (2) connection pool exhausted "
            "(check pool stats endpoint or DB connection count); (3) deadlock on a "
            "shared lock between read and write paths. Health endpoint alone is "
            "insufficient — always probe an operational endpoint in production."
        ),
        "triggers": ["service hung", "endpoint not responding", "silent deadlock"],
    },
    {
        "skill_id": "workflow.upstream_block.escalation",
        "skill_type": "procedure",
        "applies_to": ["workflow", "incident", "coordination"],
        "body": (
            "When a downstream feature hits an upstream bug or limitation: "
            "(1) file an issue on the upstream repo with a minimal reproducer; "
            "(2) tag the downstream issue as blocked-on-upstream with a link; "
            "(3) propose a temporary workaround in the downstream that doesn't "
            "create technical debt; (4) set a calendar reminder to revisit if "
            "upstream is unresponsive for >7 days. Don't fork upstream silently."
        ),
        "triggers": ["upstream bug", "blocked feature", "external dependency"],
    },
    {
        "skill_id": "review.user_visible_change.no_marketing",
        "skill_type": "rule",
        "applies_to": ["review", "writing", "product"],
        "body": (
            "For any change touching user-visible product (web UI, landing page, "
            "README hero, dashboard, demo), do NOT lead with marketing voice. "
            "Lead with the concrete user-facing change, the user problem it "
            "addresses, and the measured/observable outcome. 'Improves X' is "
            "marketing voice; 'Reduces median latency from 240ms to 90ms on the "
            "/recall endpoint at p50' is product voice. Substance first."
        ),
        "triggers": ["product change", "ui update", "demo polish", "marketing copy"],
    },
    {
        "skill_id": "workflow.session_handoff.context_distillation",
        "skill_type": "procedure",
        "applies_to": ["workflow", "meta", "memory"],
        "body": (
            "Before ending a session that did substantive work, distill: "
            "(1) what was decided (the conclusion, not the deliberation); "
            "(2) what was shipped (commit hashes, PR numbers, artifacts); "
            "(3) what remains blocked and why; "
            "(4) one sentence the next session needs to know to pick up. "
            "Crystallize this into the substrate under workflow.* — future "
            "sessions search for 'where did I leave off' and find it."
        ),
        "triggers": ["session ending", "handoff", "context distillation"],
    },
]


def load_seed_skills(provider) -> int:
    """Load the seed skills into the provider's substrate. Returns count loaded."""
    count = 0
    for entry in SEED_SKILLS:
        # The plugin's tool dispatcher accepts JSON-serializable args
        # exactly as Hermes would route an LLM tool call.
        result = provider.handle_tool_call("yantrikdb_skill_define", entry)
        if '"stored": true' in result:
            count += 1
    return count
