#!/usr/bin/env python3
"""Constellation-style animation of the skill substrate growing.

Visual companion to demo_llm.py — renders the substrate as a dark-themed
animated GIF where each skill is a glowing node, edges are semantic
similarity links, and the agent's contributions animate in over time.

Inspired by the constellation visualizer in wysie's yantrikdb-hermes-
dashboard. Standalone (doesn't require the dashboard); uses NetworkX +
matplotlib to render frames, imageio to assemble.

Run:
    pip install matplotlib networkx imageio pillow
    python3 demo_visual.py

Output: demo_visual.gif (~3-5 MB, ~12s loop)
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch
import matplotlib.patheffects as path_effects
import networkx as nx
import numpy as np
from PIL import Image
import imageio.v2 as imageio

OUTPUT = Path(__file__).parent / "demo_visual.gif"

# ── visual identity ──────────────────────────────────────────────────
BG = "#0a0e1a"            # deep space
GRID = "#1a2030"
TEXT = "#cdd6f4"
DIM = "#6c7086"
COLORS = {
    "procedure": "#94e2d5",  # teal
    "reference": "#89b4fa",  # blue
    "lesson":    "#f9e2af",  # gold (insight-shaped)
    "rule":      "#cba6f7",  # purple (norm-shaped)
    "new":       "#f38ba8",  # pink (just-created)
    "outcome":   "#a6e3a1",  # green (successful outcome)
}

# Frame parameters
W, H = 1280, 800
FPS = 12
HOLD_FRAMES = 18  # ~1.5s per key state

# Skills shape — same six seed entries from seed_skills.py
SEED = [
    ("research.preregistration.protocol",       "procedure"),
    ("deploy.allowed_kinds.extension_order",    "lesson"),
    ("incident.service.silent_deadlock_check",  "reference"),
    ("workflow.upstream_block.escalation",      "procedure"),
    ("review.user_visible_change.no_marketing", "rule"),
    ("workflow.session_handoff.context_distillation", "procedure"),
]
# Skill added by the agent in session 1
NEW_SKILL = ("release.yantrikos_clean", "procedure")


def fig_to_array(fig) -> np.ndarray:
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, dpi=100, bbox_inches="tight", pad_inches=0)
    buf.seek(0)
    img = Image.open(buf).convert("RGB")
    # Resize to fixed canvas (matplotlib tight bbox varies frame to frame)
    img = img.resize((W, H), Image.LANCZOS)
    return np.array(img)


def build_graph(skill_ids: list[tuple[str, str]]) -> nx.Graph:
    """Build a graph where edges represent applies_to overlap."""
    g = nx.Graph()
    # Synthesized similarity via shared keywords in skill_id parts.
    parts = {sid: set(sid.split(".")) for sid, _ in skill_ids}
    for sid, stype in skill_ids:
        g.add_node(sid, skill_type=stype)
    for i, (a, _) in enumerate(skill_ids):
        for b, _ in skill_ids[i+1:]:
            shared = parts[a] & parts[b]
            if shared:
                g.add_edge(a, b, weight=len(shared))
    return g


def render_frame(
    g: nx.Graph,
    *,
    title: str,
    subtitle: str = "",
    highlight: str | None = None,
    new_node: str | None = None,
    show_search: bool = False,
    outcome_node: str | None = None,
    stats_line: str = "",
) -> np.ndarray:
    fig, ax = plt.subplots(figsize=(W/100, H/100), facecolor=BG)
    ax.set_facecolor(BG)
    ax.set_xlim(-1.2, 1.2)
    ax.set_ylim(-1.0, 1.0)
    ax.axis("off")

    # Title (top left).
    ax.text(-1.15, 0.92, title, color=TEXT, fontsize=20, fontweight="bold",
            family="monospace", va="top", ha="left")
    if subtitle:
        ax.text(-1.15, 0.83, subtitle, color=DIM, fontsize=12,
                family="monospace", va="top", ha="left")
    if stats_line:
        ax.text(-1.15, -0.94, stats_line, color=TEXT, fontsize=12,
                family="monospace", va="bottom", ha="left")

    # Plugin tag (bottom right).
    ax.text(1.15, -0.94, "yantrikdb-hermes-plugin · skill_substrate",
            color=DIM, fontsize=10, family="monospace", va="bottom", ha="right")

    # Layout — circular gives a constellation feel and stays stable
    # as nodes are added (we render with the SAME positions seeded).
    nodes = list(g.nodes())
    n = len(nodes)
    pos = {}
    for i, node in enumerate(nodes):
        angle = 2 * math.pi * i / max(n, 1) - math.pi / 2
        radius = 0.55 if n <= 1 else 0.55
        pos[node] = (radius * math.cos(angle), radius * math.sin(angle) * 0.75)

    # Edges first (under nodes).
    for u, v, data in g.edges(data=True):
        x = [pos[u][0], pos[v][0]]
        y = [pos[u][1], pos[v][1]]
        ax.plot(x, y, color="#3b4252", linewidth=1.2, alpha=0.6, zorder=1)

    # Nodes with glow.
    for node in nodes:
        x, y = pos[node]
        stype = g.nodes[node].get("skill_type", "procedure")
        color = COLORS.get(stype, COLORS["procedure"])

        is_new = node == new_node
        is_hit = node == highlight
        is_outcome = node == outcome_node

        if is_new:
            color = COLORS["new"]
            size = 320
        elif is_outcome:
            color = COLORS["outcome"]
            size = 280
        elif is_hit:
            size = 280
        else:
            size = 200

        # Glow halo.
        for r, a in [(0.085, 0.10), (0.06, 0.18), (0.04, 0.32)]:
            halo = Circle((x, y), r, color=color, alpha=a, zorder=2)
            ax.add_patch(halo)
        # Core node.
        ax.scatter(x, y, s=size, c=color, edgecolors="white",
                   linewidths=1.4, zorder=3)
        # Label.
        label = node.split(".", 1)[-1] if "." in node else node
        if len(label) > 28:
            label = label[:26] + "…"
        ax.text(x, y - 0.10, label, color=TEXT, fontsize=8,
                family="monospace", ha="center", va="top", zorder=4,
                path_effects=[path_effects.withStroke(linewidth=2, foreground=BG)])
        # Skill-type chip.
        ax.text(x, y + 0.075, stype, color=DIM, fontsize=7,
                family="monospace", ha="center", va="bottom", zorder=4)

        # Outcome checkmark.
        if is_outcome:
            ax.text(x + 0.05, y + 0.05, "✓", color=COLORS["outcome"],
                    fontsize=18, fontweight="bold", ha="center", va="center",
                    zorder=5,
                    path_effects=[path_effects.withStroke(linewidth=3, foreground=BG)])

    # Search-query overlay.
    if show_search:
        ax.text(0, -0.85, "search: \"ship to PyPI\"  →  1 match",
                color=COLORS["procedure"], fontsize=14, family="monospace",
                ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.5", fc="#11151f",
                          ec=COLORS["procedure"], lw=1.5))

    plt.tight_layout(pad=0)
    arr = fig_to_array(fig)
    plt.close(fig)
    return arr


def main() -> None:
    frames: list[np.ndarray] = []

    # Stage 1 — title card / empty substrate (3s).
    g0 = nx.Graph()
    f = render_frame(
        g0,
        title="yantrikdb-hermes-plugin · LLM-driven skill lifecycle",
        subtitle="agent-authored procedures with outcome tracking",
        stats_line="skills=0, outcomes=0, conflicts=0",
    )
    frames.extend([f] * (HOLD_FRAMES * 2))

    # Stage 2 — seed appears one by one (1s each).
    current = []
    for sid, stype in SEED:
        current.append((sid, stype))
        g = build_graph(current)
        f = render_frame(
            g,
            title="Seed — prior sessions",
            subtitle=f"loading skill #{len(current)}: {sid}",
            new_node=sid,
            stats_line=f"skills={len(current)}, outcomes=0, conflicts=0",
        )
        frames.extend([f] * (FPS // 2))  # ~0.5s

    # Hold the full seed view (1.5s).
    g_seed = build_graph(SEED)
    f = render_frame(
        g_seed,
        title="Seed — 6 skills from past sessions",
        subtitle="agent inherits a lived-in substrate",
        stats_line="skills=6, outcomes=0, conflicts=0",
    )
    frames.extend([f] * HOLD_FRAMES)

    # Stage 3 — session 1: agent defines a new skill (2.5s).
    all_skills = SEED + [NEW_SKILL]
    g_after_define = build_graph(all_skills)
    for _ in range(HOLD_FRAMES + 6):
        f = render_frame(
            g_after_define,
            title="Session 1 — agent calls yantrikdb_skill_define",
            subtitle="release.yantrikos_clean  (procedure)",
            new_node=NEW_SKILL[0],
            stats_line="skills=7, outcomes=0, conflicts=0",
        )
        frames.append(f)

    # Stage 4 — session 2: search lights up the relevant node (2s).
    for _ in range(HOLD_FRAMES + 4):
        f = render_frame(
            g_after_define,
            title="Session 2 — fresh agent, calls yantrikdb_skill_search",
            subtitle='query="ship to PyPI"  top_k=5',
            highlight=NEW_SKILL[0],
            show_search=True,
            stats_line="skills=7, outcomes=0, conflicts=0",
        )
        frames.append(f)

    # Stage 5 — outcome recorded, success burst (2s).
    for _ in range(HOLD_FRAMES + 6):
        f = render_frame(
            g_after_define,
            title="Outcome recorded — yantrikdb_skill_outcome",
            subtitle="release.yantrikos_clean  succeeded=true",
            outcome_node=NEW_SKILL[0],
            stats_line="skills=7, outcomes=1, conflicts=0\n"
                       "release.yantrikos_clean: successes=1, failures=0",
        )
        frames.append(f)

    # Stage 6 — final card (3s).
    for _ in range(HOLD_FRAMES * 2):
        f = render_frame(
            g_after_define,
            title="Skill lifecycle closed",
            subtitle="authored · retrieved · outcome recorded",
            outcome_node=NEW_SKILL[0],
            stats_line="next session: this skill ranks higher · outcome history persists",
        )
        frames.append(f)

    # Assemble.
    print(f"  → rendering {len(frames)} frames @ {FPS}fps "
          f"({len(frames) / FPS:.1f}s total) …")
    imageio.mimsave(OUTPUT, frames, fps=FPS, loop=0)
    print(f"  ✓ wrote {OUTPUT.name}  ({OUTPUT.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
