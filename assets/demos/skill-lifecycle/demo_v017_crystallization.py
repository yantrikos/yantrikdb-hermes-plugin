#!/usr/bin/env python3
"""v0.4.17 demo — visible auto-skill crystallization + recall score breakdown.

Constellation animation showing the cross-session moment that v0.4.17
makes visible: session 1 defines a lesson, the session ends, and when
session 2 boots, the system prompt surfaces what session 1 learned.

The score-breakdown finale shows the per-component contributions that
sum to a result's final ranking score (similarity / recency / importance
/ decay), which v0.4.17 plumbs through unchanged from the engine.

Reuses the visual identity established in demo_visual.py (constellation
template, dark canvas, color-coded glowing skill nodes).

Run:
    pip install matplotlib networkx imageio pillow
    python3 demo_v017_crystallization.py

Output: demo_v017_crystallization.gif (~30s, target <1 MB)
"""
from __future__ import annotations

import math
import sys
from io import BytesIO
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
from matplotlib.patches import Circle, FancyBboxPatch
from PIL import Image
import imageio.v2 as imageio

OUTPUT = Path(__file__).parent / "demo_v017_crystallization.gif"

# ── visual identity ──────────────────────────────────────────────────
BG = "#0a0e1a"
GRID = "#1a2030"
TEXT = "#cdd6f4"
DIM = "#6c7086"
ACCENT = "#94e2d5"
COLORS = {
    "procedure": "#94e2d5",
    "reference": "#89b4fa",
    "lesson":    "#f9e2af",
    "rule":      "#cba6f7",
    "new":       "#f38ba8",
    "outcome":   "#a6e3a1",
}

W, H = 1280, 800
FPS = 12
HOLD = 18

# Seed substrate — same six skills as demo_visual.py for visual continuity.
SEED = [
    ("research.preregistration.protocol",            "procedure"),
    ("deploy.allowed_kinds.extension_order",         "lesson"),
    ("incident.service.silent_deadlock_check",       "reference"),
    ("workflow.upstream_block.escalation",           "procedure"),
    ("review.user_visible_change.no_marketing",      "rule"),
    ("workflow.session_handoff.context_distillation", "procedure"),
]
# The lesson the agent crystallizes during session 1 — the v0.4.17 hero.
NEW_LESSON = ("incident.deploy.allowed_kinds_race", "lesson")

# Recall scores breakdown (v0.4.17 passes these through from the engine).
SCORES = [
    ("similarity",  0.78, 0.39),
    ("recency",     0.99, 0.30),
    ("importance",  0.50, 0.39),
    ("decay",       0.50, 0.10),
]
SCORE_TOTAL = 1.18


def fig_to_array(fig) -> np.ndarray:
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, dpi=100,
                bbox_inches="tight", pad_inches=0)
    buf.seek(0)
    img = Image.open(buf).convert("RGB").resize((W, H), Image.LANCZOS)
    return np.array(img)


def build_graph(skill_ids: list[tuple[str, str]]) -> nx.Graph:
    g = nx.Graph()
    parts = {sid: set(sid.split(".")) for sid, _ in skill_ids}
    for sid, stype in skill_ids:
        g.add_node(sid, skill_type=stype)
    for i, (a, _) in enumerate(skill_ids):
        for b, _ in skill_ids[i+1:]:
            shared = parts[a] & parts[b]
            if shared:
                g.add_edge(a, b, weight=len(shared))
    return g


def _positions(nodes: list[str]) -> dict[str, tuple[float, float]]:
    n = len(nodes)
    pos = {}
    for i, node in enumerate(nodes):
        angle = 2 * math.pi * i / max(n, 1) - math.pi / 2
        pos[node] = (0.55 * math.cos(angle), 0.55 * math.sin(angle) * 0.75)
    return pos


def _draw_axes(title: str, subtitle: str, stats_line: str):
    fig, ax = plt.subplots(figsize=(W/100, H/100), facecolor=BG)
    ax.set_facecolor(BG)
    ax.set_xlim(-1.2, 1.2)
    ax.set_ylim(-1.0, 1.0)
    ax.axis("off")
    ax.text(-1.15, 0.92, title, color=TEXT, fontsize=20, fontweight="bold",
            family="monospace", va="top", ha="left")
    if subtitle:
        ax.text(-1.15, 0.83, subtitle, color=DIM, fontsize=12,
                family="monospace", va="top", ha="left")
    if stats_line:
        ax.text(-1.15, -0.94, stats_line, color=TEXT, fontsize=12,
                family="monospace", va="bottom", ha="left")
    ax.text(1.15, -0.94, "yantrikdb-hermes-plugin v0.4.17",
            color=DIM, fontsize=10, family="monospace",
            va="bottom", ha="right")
    return fig, ax


def render_constellation(
    g: nx.Graph,
    *,
    title: str,
    subtitle: str = "",
    highlight: str | None = None,
    new_node: str | None = None,
    stats_line: str = "",
    prompt_block_lines: list[str] | None = None,
    prompt_block_alpha: float = 1.0,
) -> np.ndarray:
    fig, ax = _draw_axes(title, subtitle, stats_line)

    nodes = list(g.nodes())
    pos = _positions(nodes)

    for u, v, _ in g.edges(data=True):
        x = [pos[u][0], pos[v][0]]
        y = [pos[u][1], pos[v][1]]
        ax.plot(x, y, color="#3b4252", linewidth=1.2, alpha=0.6, zorder=1)

    for node in nodes:
        x, y = pos[node]
        stype = g.nodes[node].get("skill_type", "procedure")
        color = COLORS.get(stype, COLORS["procedure"])
        is_new = node == new_node
        is_hit = node == highlight
        if is_new:
            color = COLORS["new"]
            size = 320
        elif is_hit:
            size = 320
        else:
            size = 200
        for r, a in [(0.085, 0.10), (0.06, 0.18), (0.04, 0.32)]:
            ax.add_patch(Circle((x, y), r, color=color, alpha=a, zorder=2))
        ax.scatter(x, y, s=size, c=color, edgecolors="white",
                   linewidths=1.4, zorder=3)
        label = node.split(".", 1)[-1] if "." in node else node
        if len(label) > 28:
            label = label[:26] + "…"
        ax.text(x, y - 0.10, label, color=TEXT, fontsize=8,
                family="monospace", ha="center", va="top", zorder=4,
                path_effects=[path_effects.withStroke(linewidth=2, foreground=BG)])
        ax.text(x, y + 0.075, stype, color=DIM, fontsize=7,
                family="monospace", ha="center", va="bottom", zorder=4)
        if is_hit and prompt_block_lines:
            # Draw a connecting line from the prompt-block panel down to
            # the highlighted node, showing how the surfaced reference
            # corresponds to a substrate entry.
            ax.plot([-0.65, x], [0.48, y], color=COLORS["lesson"],
                    linewidth=1.2, alpha=0.6 * prompt_block_alpha,
                    linestyle="--", zorder=2)

    if prompt_block_lines:
        # Top-right panel showing the system_prompt_block surfacing the
        # prior session's learning. Alpha controls fade-in.
        x0, y0, w, h = 0.05, 0.20, 1.05, 0.55
        ax.add_patch(FancyBboxPatch(
            (x0, y0), w, h,
            boxstyle="round,pad=0.02",
            linewidth=1.5,
            edgecolor=COLORS["lesson"],
            facecolor="#11151f",
            alpha=prompt_block_alpha,
            zorder=5,
        ))
        for i, line in enumerate(prompt_block_lines):
            ax.text(x0 + 0.03, y0 + h - 0.08 - i * 0.075,
                    line,
                    color=TEXT if i == 0 else DIM if line.startswith("  ")
                    else COLORS["lesson"] if line.startswith("- ")
                    else TEXT,
                    fontsize=11 if i == 0 else 10,
                    fontweight="bold" if i == 0 else "normal",
                    family="monospace", va="top", ha="left",
                    alpha=prompt_block_alpha, zorder=6)

    plt.tight_layout(pad=0)
    arr = fig_to_array(fig)
    plt.close(fig)
    return arr


def render_persistence_card(progress: float) -> np.ndarray:
    """Show the file-write moment between sessions."""
    fig, ax = _draw_axes(
        "Session 1 ended — skill persisted across the boundary",
        "the plugin records this skill so the next session can see it",
        f"$HERMES_HOME/yantrikdb-recent-skills.json   {('●' * int(progress * 20)).ljust(20, '·')}",
    )

    # File icon (simple rounded rectangle with corner fold).
    panel_w, panel_h = 0.95, 0.45
    panel_x, panel_y = -panel_w / 2, -0.15
    ax.add_patch(FancyBboxPatch(
        (panel_x, panel_y), panel_w, panel_h,
        boxstyle="round,pad=0.02", linewidth=1.5,
        edgecolor=COLORS["lesson"], facecolor="#11151f", zorder=2,
    ))

    json_text = [
        "[",
        "  {",
        '    "skill_id": "incident.deploy.allowed_kinds_race",',
        '    "skill_type": "lesson",',
        '    "applies_to": ["incident"],',
        f'    "ts": 1748395012.{int(progress * 999):03d},',
        '    "session_id": "sess-1"',
        "  }",
        "]",
    ]
    visible = max(1, int(progress * len(json_text)))
    for i, line in enumerate(json_text[:visible]):
        c = COLORS["lesson"] if '"skill_id"' in line or '"skill_type"' in line else TEXT
        ax.text(panel_x + 0.03, panel_y + panel_h - 0.05 - i * 0.045,
                line, color=c, fontsize=10, family="monospace",
                va="top", ha="left", zorder=3)

    plt.tight_layout(pad=0)
    arr = fig_to_array(fig)
    plt.close(fig)
    return arr


def render_scores_card(progress: float) -> np.ndarray:
    """Animated horizontal-bar chart for the recall score breakdown."""
    fig, ax = _draw_axes(
        "v0.4.17 bonus — recall now exposes score components",
        "score = Σ contributions across similarity / recency / importance / decay",
        f"yantrikdb_recall  query=\"deploy race condition\"  →  total score = {SCORE_TOTAL:.2f}",
    )

    # Result-text header.
    ax.text(-1.15, 0.65,
            'result[0].text = "agent learned: never resolve allowed_kinds '
            'before deploy event"',
            color=ACCENT, fontsize=11, family="monospace",
            va="top", ha="left", zorder=3)
    ax.text(-1.15, 0.58,
            'rid = "019e7229-0819-7536-905c-c38219d5e5bb"   '
            'why_retrieved = ["high similarity", "recently created"]',
            color=DIM, fontsize=10, family="monospace",
            va="top", ha="left", zorder=3)

    # Bars. Two-column layout: raw component value (left bar) and the
    # weighted contribution to final score (right number).
    bar_x0 = -0.85
    bar_max_w = 1.30
    row_h = 0.18
    top_y = 0.30
    label_color = TEXT
    track_color = "#1f2335"

    for i, (label, value, contrib) in enumerate(SCORES):
        y = top_y - i * row_h
        # Component label.
        ax.text(-1.15, y, label, color=label_color, fontsize=12,
                family="monospace", va="center", ha="left", zorder=3)
        # Background track.
        ax.add_patch(FancyBboxPatch(
            (bar_x0, y - 0.04), bar_max_w, 0.08,
            boxstyle="round,pad=0.005", linewidth=0,
            facecolor=track_color, zorder=2,
        ))
        # Filled bar — animates from 0 to value across the progress range.
        # Stagger so bars fill one after another.
        stagger_start = i * 0.15
        stagger = max(0.0, min(1.0, (progress - stagger_start) / 0.5))
        fill_w = bar_max_w * value * stagger
        color = COLORS["procedure"] if label == "similarity" else \
                COLORS["lesson"] if label == "recency" else \
                COLORS["reference"] if label == "importance" else \
                COLORS["new"]
        ax.add_patch(FancyBboxPatch(
            (bar_x0, y - 0.04), max(0.001, fill_w), 0.08,
            boxstyle="round,pad=0.005", linewidth=0,
            facecolor=color, zorder=3, alpha=0.9,
        ))
        # Value + contribution text.
        if stagger > 0.05:
            ax.text(bar_x0 + bar_max_w + 0.04, y,
                    f"{value:.2f}  →  contribution {contrib:+.2f}",
                    color=color, fontsize=11, family="monospace",
                    va="center", ha="left", alpha=stagger, zorder=4)

    # Total line.
    total_y = top_y - len(SCORES) * row_h - 0.05
    total_alpha = max(0.0, min(1.0, (progress - 0.85) / 0.15))
    if total_alpha > 0:
        ax.plot([bar_x0, bar_x0 + bar_max_w + 0.6], [total_y + 0.08] * 2,
                color=DIM, linewidth=0.8, alpha=total_alpha)
        ax.text(-1.15, total_y, "total score", color=TEXT, fontsize=13,
                fontweight="bold", family="monospace",
                va="center", ha="left", alpha=total_alpha, zorder=4)
        ax.text(bar_x0 + bar_max_w + 0.04, total_y, f"{SCORE_TOTAL:.2f}",
                color=COLORS["outcome"], fontsize=13, fontweight="bold",
                family="monospace", va="center", ha="left",
                alpha=total_alpha, zorder=4)

    plt.tight_layout(pad=0)
    arr = fig_to_array(fig)
    plt.close(fig)
    return arr


def main() -> None:
    frames: list[np.ndarray] = []

    # ── Beat 1: title card (3s) ──────────────────────────────────────
    g_empty = nx.Graph()
    f = render_constellation(
        g_empty,
        title="yantrikdb-hermes-plugin · v0.4.17",
        subtitle="visible auto-skill crystallization · score breakdown",
        stats_line="what session 1 learned, session 2 sees",
    )
    frames.extend([f] * (HOLD * 2))

    # ── Beat 2: session 1 — agent crystallizes a lesson (5s) ─────────
    g_seed = build_graph(SEED)
    for _ in range(HOLD):
        frames.append(render_constellation(
            g_seed,
            title="Session 1 — agent investigating a deploy incident",
            subtitle="6 prior skills loaded; reasoning over substrate…",
            stats_line="skills=6, last_define=—",
        ))
    g_after = build_graph(SEED + [NEW_LESSON])
    for _ in range(HOLD + 6):
        frames.append(render_constellation(
            g_after,
            title="Session 1 — yantrikdb_skill_define",
            subtitle="incident.deploy.allowed_kinds_race  (lesson)",
            new_node=NEW_LESSON[0],
            stats_line="skills=7, stored=true, persist=yantrikdb-recent-skills.json",
        ))

    # ── Beat 3: session ended, persistence card (3s) ─────────────────
    persist_steps = HOLD + 6
    for i in range(persist_steps):
        progress = (i + 1) / persist_steps
        frames.append(render_persistence_card(progress))

    # ── Beat 4: session 2 boots, prompt block fades in (5s) ──────────
    prompt_lines = [
        "## Recently learned skills",
        "",
        "- `incident.deploy.allowed_kinds_race` (lesson)",
        "    scope=incident — 3h ago",
        "",
        "These were defined in prior sessions. If your task",
        "matches any, call yantrikdb_skill_search for the body.",
    ]
    fade_steps = 8
    for i in range(fade_steps):
        alpha = (i + 1) / fade_steps
        frames.append(render_constellation(
            g_after,
            title="Session 2 — fresh model boots",
            subtitle="system_prompt_block() returns…",
            stats_line="system prompt augmented with prior-session learning",
            prompt_block_lines=prompt_lines,
            prompt_block_alpha=alpha,
        ))
    for _ in range(HOLD + 8):
        frames.append(render_constellation(
            g_after,
            title="Session 2 — the new model sees what session 1 learned",
            subtitle="the lesson surfaces in this session's system prompt",
            highlight=NEW_LESSON[0],
            stats_line="cross-session crystallization: visible · automatic · bounded by TTL",
            prompt_block_lines=prompt_lines,
            prompt_block_alpha=1.0,
        ))

    # ── Beat 5: recall + scores bar chart (6s) ───────────────────────
    score_steps = HOLD * 3
    for i in range(score_steps):
        progress = (i + 1) / score_steps
        frames.append(render_scores_card(progress))
    # Hold the final chart.
    final_scores = render_scores_card(1.0)
    frames.extend([final_scores] * HOLD)

    # ── Beat 6: closing card (4s) ────────────────────────────────────
    for _ in range(HOLD * 2 + 4):
        frames.append(render_constellation(
            g_after,
            title="v0.4.17 — what the substrate did is now visible",
            subtitle="auto-crystallization · transparent ranking · zero config",
            highlight=NEW_LESSON[0],
            stats_line="pip install -U yantrikdb-hermes-plugin",
        ))

    secs = len(frames) / FPS
    print(f"  → rendering {len(frames)} frames @ {FPS}fps ({secs:.1f}s) …",
          file=sys.stderr)
    imageio.mimsave(OUTPUT, frames, fps=FPS, loop=0)
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"  ✓ wrote {OUTPUT.name}  ({size_kb:.0f} KB, {secs:.1f}s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
