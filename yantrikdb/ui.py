"""v0.5 Wave C bundled UI — pure-stdlib HTTP server + single-page constellation.

``yantrikdb-hermes ui [--port 8767] [--open]`` opens a localhost web
inspector that shows three sections against the active substrate:

1. **Constellation** — memories rendered as glowing nodes, semantic
   neighbours linked by faint edges. Stripped-down version of the
   constellation in `assets/demos/skill-lifecycle/demo_visual.py` and
   wysie's full dashboard. Read-only.
2. **Recent skills** — last N skills surfaced from
   ``$HERMES_HOME/yantrikdb-recent-skills.json`` (the v0.4.17 carry-
   forward record) plus a recall pass over the skill namespace.
3. **Unresolved conflicts** — current ``conflicts()`` output, displayed
   as A/B pairs with the conflict_id for follow-up via
   ``yantrikdb_resolve_conflict``.

Scope per design (`docs/v0.5-design.md` §C1): read-only, single page,
3 sections, ~250 LOC total. **NOT** a replacement for wysie's full
dashboard — this is the *first-10-minutes-after-install* tool that
ships in the wheel so users can see the substrate within 30 seconds of
``pip install``.
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import threading
import webbrowser
from pathlib import Path
from typing import Any

__all__ = ["serve", "build_snapshot"]


def _load_client() -> tuple[Any, str]:
    """Connect to the active substrate via the same config the provider uses."""
    from .client import YantrikDBConfig
    from .embedded import EmbeddedYantrikDBClient

    hermes_home = os.environ.get("HERMES_HOME")
    cfg = YantrikDBConfig.load(Path(hermes_home) if hermes_home else None)
    if cfg.mode != "embedded":
        raise RuntimeError(
            "bundled UI only supports embedded mode. For HTTP-cluster "
            "deployments use wysie's full dashboard."
        )
    client = EmbeddedYantrikDBClient(cfg)
    namespace = cfg.namespace or "hermes"
    return client, namespace


def build_snapshot() -> dict[str, Any]:
    """One-shot substrate snapshot for the UI (called per page refresh)."""
    client, namespace = _load_client()

    # Memories — pull recent via broad probes, dedupe by rid.
    seen: set[str] = set()
    memories: list[dict[str, Any]] = []
    for q in ("user", "agent", "is", "prefers", "the"):
        try:
            resp = client.recall(query=q, top_k=20, namespace=namespace)
        except Exception:
            continue
        for r in (resp.get("results") or []):
            rid = r.get("rid")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            memories.append({
                "rid": rid,
                "text": (r.get("text") or "")[:200],
                "score": r.get("score"),
                "source": (r.get("metadata") or {}).get("source", ""),
                "domain": r.get("domain") or (r.get("metadata") or {}).get("domain", ""),
            })

    # Conflicts
    try:
        conflicts_resp = client.conflicts(namespace=namespace)
        conflicts = conflicts_resp.get("conflicts", []) or []
    except Exception:
        conflicts = []

    # Recently-defined skills (the v0.4.17 record under $HERMES_HOME)
    recent_skills: list[dict[str, Any]] = []
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        path = Path(hermes_home) / "yantrikdb-recent-skills.json"
        if path.exists():
            try:
                recent_skills = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                recent_skills = []

    # Engine stats for the header
    try:
        stats = client.stats(namespace=namespace)
    except Exception:
        stats = {}

    return {
        "namespace": namespace,
        "memories": memories,
        "conflicts": conflicts,
        "recent_skills": recent_skills,
        "stats": stats,
    }


def _render_html(snapshot: dict[str, Any]) -> str:
    """Render the single-page UI. Inline HTML/CSS/JS — no external assets."""
    return _PAGE_TEMPLATE.replace(
        "__SNAPSHOT_JSON__",
        json.dumps(snapshot, default=str),
    )


_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>YantrikDB · substrate inspector</title>
<style>
  :root {
    --bg: #0a0e1a; --grid: #1a2030; --text: #cdd6f4; --dim: #6c7086;
    --proc: #94e2d5; --ref: #89b4fa; --lesson: #f9e2af; --rule: #cba6f7;
    --new: #f38ba8; --ok: #a6e3a1; --warn: #f9e2af;
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; background: var(--bg); color: var(--text);
         font-family: "JetBrains Mono", "Fira Code", Consolas, monospace;
         font-size: 13px; line-height: 1.45; }
  h1 { font-size: 18px; margin: 0 0 4px; font-weight: 600; }
  h2 { font-size: 14px; margin: 24px 0 8px; color: var(--dim); font-weight: 500;
       text-transform: uppercase; letter-spacing: 0.05em; }
  .sub { color: var(--dim); margin-bottom: 24px; font-size: 11px; }
  .stats { display: flex; gap: 16px; flex-wrap: wrap; padding: 12px 16px;
           background: #11151f; border-radius: 6px; margin-bottom: 24px; }
  .stat { display: flex; flex-direction: column; }
  .stat .v { color: var(--text); font-size: 16px; font-weight: 600; }
  .stat .l { color: var(--dim); font-size: 10px; text-transform: uppercase; }
  svg.constellation { display: block; width: 100%; height: 480px;
                      background: #11151f; border-radius: 6px;
                      margin-bottom: 24px; }
  .node circle { stroke: white; stroke-width: 1.2; opacity: 0.9; }
  .node text { fill: var(--text); font-size: 9px; font-family: inherit;
               pointer-events: none; }
  .edge { stroke: #3b4252; stroke-width: 0.8; opacity: 0.4; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
  th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--grid);
           vertical-align: top; }
  th { color: var(--dim); text-transform: uppercase; font-size: 10px;
       letter-spacing: 0.05em; font-weight: 500; }
  td { color: var(--text); font-size: 12px; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 99px;
          font-size: 10px; }
  .pill-extracted { background: #2d1f1f; color: var(--new); }
  .pill-skill { background: #1f2d2d; color: var(--proc); }
  .empty { color: var(--dim); font-style: italic; padding: 16px 0; }
  .conflict { border-left: 2px solid var(--new); padding: 8px 12px;
              margin-bottom: 8px; background: #11151f; }
  .conflict .id { color: var(--dim); font-size: 10px; }
  .conflict .pair { display: flex; gap: 16px; margin-top: 4px; }
  .conflict .pair > div { flex: 1; padding: 6px 10px; background: #0a0e1a;
                          border-radius: 4px; }
  .conflict .label { color: var(--dim); font-size: 10px; margin-bottom: 2px; }
  footer { color: var(--dim); font-size: 11px; margin-top: 32px;
           padding-top: 16px; border-top: 1px solid var(--grid); }
  a { color: var(--ref); text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>YantrikDB · substrate inspector</h1>
<div class="sub" id="sub"></div>
<div class="stats" id="stats"></div>

<h2>Constellation</h2>
<svg class="constellation" id="constellation"></svg>

<h2>Recently learned skills</h2>
<table id="skills-table"><thead><tr>
  <th>skill_id</th><th>type</th><th>scope</th><th>age</th>
</tr></thead><tbody></tbody></table>
<div class="empty" id="skills-empty" style="display:none">
  No skills defined recently. Run with YANTRIKDB_SKILLS_ENABLED=true and call
  yantrikdb_skill_define to record one.
</div>

<h2>Unresolved contradictions</h2>
<div id="conflicts"></div>
<div class="empty" id="conflicts-empty" style="display:none">
  No unresolved conflicts. Run yantrikdb_think() to surface new ones.
</div>

<footer>
  Read-only UI from yantrikdb-hermes-plugin. For full dashboard
  (multi-DB, mutations, auth) install
  <a href="https://github.com/wysie/yantrikdb-hermes-dashboard" target="_blank">wysie's dashboard</a>.
  &nbsp;|&nbsp; <a href="/api/snapshot">raw JSON</a>
</footer>

<script>
const data = __SNAPSHOT_JSON__;

document.getElementById("sub").textContent =
  "namespace: " + (data.namespace || "(unset)") + " · live snapshot at page load";

const s = data.stats || {};
const stats = [
  ["memories", s.active_memories],
  ["entities", s.entities],
  ["edges", s.edges],
  ["conflicts", s.open_conflicts],
  ["operations", s.operations],
  ["tombstoned", s.tombstoned_memories],
];
document.getElementById("stats").innerHTML = stats.map(([l, v]) =>
  `<div class="stat"><div class="v">${v ?? "?"}</div><div class="l">${l}</div></div>`
).join("");

// Constellation: simple circular layout over the recent memories.
const svg = document.getElementById("constellation");
const W = svg.clientWidth || 1000, H = 480;
const memories = (data.memories || []).slice(0, 40);
const cx = W / 2, cy = H / 2;
const r = Math.min(W, H) * 0.4;

// Edges: connect each memory to its nearest neighbours by domain match
const byDomain = {};
memories.forEach((m, i) => {
  const d = m.domain || m.source || "general";
  (byDomain[d] = byDomain[d] || []).push(i);
});

const edges = [];
for (const grp of Object.values(byDomain)) {
  for (let i = 0; i < grp.length - 1; i++) {
    edges.push([grp[i], grp[i + 1]]);
  }
}

const COLOR = {
  "preference": "#94e2d5", "people": "#89b4fa", "work": "#f9e2af",
  "reference": "#cba6f7", "extracted": "#f38ba8",
  "general": "#cdd6f4",
};

const pts = memories.map((_, i) => {
  const a = (2 * Math.PI * i) / Math.max(memories.length, 1) - Math.PI / 2;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a) * 0.75];
});

let html = "";
for (const [a, b] of edges) {
  html += `<line class="edge" x1="${pts[a][0]}" y1="${pts[a][1]}" `
        + `x2="${pts[b][0]}" y2="${pts[b][1]}"/>`;
}
memories.forEach((m, i) => {
  const [x, y] = pts[i];
  const color = COLOR[m.source === "extracted" ? "extracted" : (m.domain || "general")]
              || "#cdd6f4";
  const text = (m.text || "").slice(0, 28);
  html += `<g class="node">`
        + `<circle cx="${x}" cy="${y}" r="6" fill="${color}"/>`
        + `<text x="${x}" y="${y + 16}" text-anchor="middle">${text.replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))}</text>`
        + `</g>`;
});
if (!memories.length) {
  html = `<text x="${cx}" y="${cy}" text-anchor="middle" fill="#6c7086" font-size="12">`
       + `Substrate is empty. Add memories via yantrikdb_remember.</text>`;
}
svg.innerHTML = html;

// Skills table
const skillsBody = document.querySelector("#skills-table tbody");
const skills = data.recent_skills || [];
if (!skills.length) {
  document.getElementById("skills-table").style.display = "none";
  document.getElementById("skills-empty").style.display = "block";
} else {
  skillsBody.innerHTML = skills.slice(-10).reverse().map(sk => {
    const age = sk.ts ? formatAge(Date.now() / 1000 - sk.ts) : "—";
    const applies = (sk.applies_to || []).join(", ");
    return `<tr>`
      + `<td><span class="pill pill-skill">${sk.skill_id || "?"}</span></td>`
      + `<td>${sk.skill_type || "?"}</td>`
      + `<td>${applies}</td>`
      + `<td>${age}</td>`
      + `</tr>`;
  }).join("");
}

// Conflicts
const conflictsDiv = document.getElementById("conflicts");
const conflicts = data.conflicts || [];
if (!conflicts.length) {
  document.getElementById("conflicts-empty").style.display = "block";
} else {
  conflictsDiv.innerHTML = conflicts.slice(0, 10).map(c => {
    const id = c.conflict_id || c.rid || "?";
    const a = c.text_a || c.a || "";
    const b = c.text_b || c.b || "";
    return `<div class="conflict">`
      + `<div class="id">${id}</div>`
      + `<div class="pair">`
        + `<div><div class="label">A</div>${escape(a)}</div>`
        + `<div><div class="label">B</div>${escape(b)}</div>`
      + `</div></div>`;
  }).join("");
}

function escape(s) {
  return String(s).replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
}

function formatAge(secs) {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}
</script>
</body>
</html>
"""


class _UIHandler(http.server.BaseHTTPRequestHandler):
    """Serve the index page and a JSON snapshot endpoint. Read-only."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Quiet — don't spam stderr per request.
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/" or path == "/index.html":
                snapshot = build_snapshot()
                body = _render_html(snapshot).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/snapshot":
                snapshot = build_snapshot()
                body = json.dumps(snapshot, default=str, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            body = f"500 internal: {type(e).__name__}: {e}".encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def serve(host: str = "127.0.0.1", port: int = 8767, open_browser: bool = False) -> None:
    """Block serving the UI at ``http://host:port/``."""
    addr = (host, port)
    httpd = socketserver.TCPServer(addr, _UIHandler)
    url = f"http://{host}:{port}/"
    print(f"YantrikDB substrate inspector listening at {url}")
    print("Ctrl-C to stop.")
    if open_browser:
        threading.Thread(
            target=lambda: webbrowser.open(url), daemon=True,
        ).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        httpd.server_close()
