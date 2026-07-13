#!/usr/bin/env python3
"""Demo: the self-directing substrate (v0.8).

Shows the loop no other Hermes memory provider can do -- the memory notices
what it doesn't know, queues the work, hands the agent an agenda, and closes
the loop when the gap is answered:

    ask (poorly answered)  ->  knowledge gap
    session end            ->  gap becomes a durable task
    session start          ->  "your memory's agenda" surfaces it
    agent learns + records ->  gap fades, task marked done

Runnable against a real embedded engine (needs yantrikdb>=0.9.0). No API key.

    python demos/self_directing_memory.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Reuse the benchmark harness to load the plugin + an embedded engine.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
from _bootstrap import make_provider  # noqa: E402


def hr(title):
    print("\n" + "-" * 62 + "\n  " + title + "\n" + "-" * 62)


def call(p, tool, args):
    return json.loads(p.handle_tool_call(tool, args))


def main() -> int:
    p = make_provider(env={
        "YANTRIKDB_AUTO_GAP_TASKS": "true",
        "YANTRIKDB_SURFACE_AGENDA": "true",
        "YANTRIKDB_GAP_TASK_MIN_COUNT": "2",
        # dim-64 potion-2M scores unanswered queries ~0.6; loosen the gap
        # threshold so the demo's genuinely-absent topics register.
        "YANTRIKDB_GAP_MAX_AVG_TOP_SCORE": "0.75",
        "YANTRIKDB_AUTO_THINK_ON_SESSION_END": "true",
    })

    hr("1. A small memory, and questions it can't answer")
    for fact in ["We use PostgreSQL for the billing database.",
                 "Alice Tan leads DevOps and owns the CI/CD pipeline."]:
        call(p, "yantrikdb_remember", {"text": fact, "importance": 0.8})
    print("Seeded 2 memories.\n")
    unanswerable = [
        "what is our kubernetes ingress TLS configuration",
        "what is the on-call escalation path after hours",
    ]
    for q in unanswerable:
        for _ in range(3):          # asked repeatedly, answered poorly -> demand
            call(p, "yantrikdb_recall", {"query": q, "top_k": 3})
        print("  agent asked  ->  " + repr(q) + "  (no good answer)")

    hr("2. Session ends: the memory turns its gaps into tasks")
    p.on_session_end([])            # runs think() + auto_gap_tasks
    time.sleep(0.2)
    gaps = call(p, "yantrikdb_knowledge_gaps",
                {"min_count": 2, "max_avg_top_score": 0.75})
    print("  knowledge gaps detected: " + str(gaps.get("count")))
    tasks = call(p, "yantrikdb_tasks", {"action": "list"})
    print("  tasks auto-created: " + str(tasks.get("count")))
    for t in tasks.get("tasks", []):
        print("    - " + str(t.get("title")) + "   [" + str(t.get("status")) + "]")

    hr("3. Next session opens with the memory's own agenda")
    print(p._format_agenda_block() or "(agenda empty)")

    hr("4. The agent learns one thing: the loop closes")
    learned = ("Our Kubernetes ingress terminates TLS at the nginx ingress "
               "controller using cert-manager with Let's Encrypt.")
    call(p, "yantrikdb_remember", {"text": learned, "importance": 0.8})
    print("  learned + recorded:  " + repr(learned[:60] + "..."))
    ans = call(p, "yantrikdb_recall", {"query": unanswerable[0], "top_k": 2})
    top = ans.get("results", [{}])[0] if ans.get("results") else {}
    print("  recall now answers:  " + repr((top.get("text") or "")[:60] + "..."))
    for t in tasks.get("tasks", []):
        if "ingress" in (t.get("title") or "").lower():
            call(p, "yantrikdb_tasks",
                 {"action": "update", "task_id": t["id"], "status": "done"})
            print("  task closed:  " + str(t.get("title")))

    hr("5. The agenda shrank: one loop closed, one still open")
    print(p._format_agenda_block() or "(agenda empty)")
    print("\n[done] The substrate directed its own maintenance. No other "
          "Hermes memory provider closes this loop.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
