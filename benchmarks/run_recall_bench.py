#!/usr/bin/env python3
"""Reproducible recall-quality benchmark for the YantrikDB Hermes plugin.

Spins up a real embedded YantrikDB in a temp dir (bundled potion-2M
embedder, deterministic), ingests a curated memory corpus, runs each query
through the real provider recall path, and scores:

  - recall@k       : fraction of queries where a gold memory is in the top-k
  - MRR            : mean reciprocal rank of the first gold hit
  - answer@k       : fraction where a top-k result text contains the gold
                     answer substring (a stricter, content-level check)

With ``--reinforce`` it additionally measures the v0.6 self-tuning lift:
run the queries, reinforce the gold memory each query relied on, re-run,
and report the MRR / recall@1 delta. This is the number that proves the
feedback loop actually improves ranking.

Usage:
    python benchmarks/run_recall_bench.py
    python benchmarks/run_recall_bench.py --reinforce
    python benchmarks/run_recall_bench.py --json out.json --markdown out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import make_provider  # noqa: E402

DATASET = Path(__file__).resolve().parent / "dataset.json"


def _ingest(provider: Any, corpus: list[dict[str, Any]]) -> dict[str, str]:
    """Store each corpus item; return author-id -> engine-rid map."""
    id_to_rid: dict[str, str] = {}
    for item in corpus:
        out = json.loads(provider.handle_tool_call("yantrikdb_remember", {
            "text": item["text"],
            "importance": item.get("importance", 0.5),
            "domain": item.get("domain", "general"),
        }))
        rid = out.get("rid")
        if rid:
            id_to_rid[item["id"]] = rid
    return id_to_rid


def _recall(provider: Any, query: str, top_k: int,
            reinforce: list[str] | None = None) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"query": query, "top_k": top_k}
    if reinforce:
        args["reinforce"] = reinforce
    out = json.loads(provider.handle_tool_call("yantrikdb_recall", args))
    return out.get("results", []) or []


def _gold_rank(results: list[dict[str, Any]], gold_rids: set[str]) -> int | None:
    """1-based rank of the first gold rid in results, or None if absent."""
    for i, r in enumerate(results, start=1):
        if r.get("rid") in gold_rids:
            return i
    return None


def _answer_hit(results: list[dict[str, Any]], substring: str, k: int) -> bool:
    sub = (substring or "").lower()
    if not sub:
        return False
    return any(sub in (r.get("text") or "").lower() for r in results[:k])


def evaluate(provider: Any, dataset: dict[str, Any], id_to_rid: dict[str, str],
             *, reinforce_after: bool = False) -> dict[str, Any]:
    """Run every query once and aggregate metrics. Optionally reinforce the
    gold memory of each query after scoring it (used for the lift A/B)."""
    k_values = dataset.get("k_values", [1, 3, 5, 10])
    max_k = max(k_values)
    queries = dataset["queries"]

    recall_at: dict[int, int] = {k: 0 for k in k_values}
    answer_at: dict[int, int] = {k: 0 for k in k_values}
    rr_sum = 0.0
    per_query: list[dict[str, Any]] = []

    for q in queries:
        gold_rids = {id_to_rid[g] for g in q["gold_ids"] if g in id_to_rid}
        results = _recall(provider, q["q"], max_k)
        rank = _gold_rank(results, gold_rids)
        rr = (1.0 / rank) if rank else 0.0
        rr_sum += rr
        for k in k_values:
            if rank is not None and rank <= k:
                recall_at[k] += 1
            if _answer_hit(results, q.get("gold_substring", ""), k):
                answer_at[k] += 1
        per_query.append({"q": q["q"], "rank": rank, "rr": round(rr, 4)})
        if reinforce_after and gold_rids:
            # Simulate the agent marking the memory it actually used.
            _recall(provider, q["q"], max_k, reinforce=list(gold_rids))

    n = len(queries)
    return {
        "n_queries": n,
        "recall_at_k": {k: round(recall_at[k] / n, 4) for k in k_values},
        "answer_at_k": {k: round(answer_at[k] / n, 4) for k in k_values},
        "mrr": round(rr_sum / n, 4),
        "per_query": per_query,
    }


def _markdown(report: dict[str, Any]) -> str:
    base = report["baseline"]
    k_values = sorted(int(k) for k in base["recall_at_k"])
    lines = [
        "# YantrikDB recall benchmark",
        "",
        f"Dataset: `{report['dataset']}` - {base['n_queries']} queries, "
        f"{report['corpus_size']} memories. Embedder: bundled potion-2M.",
        "",
        "| metric | " + " | ".join(f"@{k}" for k in k_values) + " |",
        "|---|" + "---|" * len(k_values),
        "| recall | " + " | ".join(
            f"{base['recall_at_k'][str(k)] if str(k) in base['recall_at_k'] else base['recall_at_k'][k]:.3f}"
            for k in k_values) + " |",
        "| answer-containment | " + " | ".join(
            f"{base['answer_at_k'][str(k)] if str(k) in base['answer_at_k'] else base['answer_at_k'][k]:.3f}"
            for k in k_values) + " |",
        "",
        f"**MRR: {base['mrr']:.3f}**",
    ]
    if "self_tuning" in report:
        st = report["self_tuning"]
        lines += [
            "",
            "## Self-tuning lift (v0.6 Wave F)",
            "",
            "| pass | MRR | recall@1 |",
            "|---|---|---|",
            f"| before reinforcement | {st['before']['mrr']:.3f} | "
            f"{st['before']['recall_at_k'].get('1', st['before']['recall_at_k'].get(1)):.3f} |",
            f"| after reinforcement | {st['after']['mrr']:.3f} | "
            f"{st['after']['recall_at_k'].get('1', st['after']['recall_at_k'].get(1)):.3f} |",
            "",
            f"**MRR lift: {st['mrr_lift']:+.3f}** "
            f"({st['mrr_lift_pct']:+.1f}%)",
        ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="YantrikDB recall benchmark")
    ap.add_argument("--reinforce", action="store_true",
                    help="also measure the self-tuning MRR lift")
    ap.add_argument("--json", type=str, default="",
                    help="write the full report JSON to this path")
    ap.add_argument("--markdown", type=str, default="",
                    help="write the markdown summary to this path")
    ap.add_argument("--dataset", type=str, default=str(DATASET))
    args = ap.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))

    # Baseline: self-tuning OFF (default), pure engine ranking.
    provider = make_provider(env={"YANTRIKDB_SELF_TUNING_RECALL": "false"})
    id_to_rid = _ingest(provider, dataset["corpus"])
    baseline = evaluate(provider, dataset, id_to_rid)

    report: dict[str, Any] = {
        "dataset": dataset.get("name", "unknown"),
        "corpus_size": len(dataset["corpus"]),
        "baseline": baseline,
    }

    if args.reinforce:
        # Fresh provider with self-tuning ON. Pass 1 scores + reinforces the
        # gold memory of each query; pass 2 re-scores to measure the lift.
        st_provider = make_provider(env={"YANTRIKDB_SELF_TUNING_RECALL": "true"})
        st_ids = _ingest(st_provider, dataset["corpus"])
        before = evaluate(st_provider, dataset, st_ids, reinforce_after=True)
        after = evaluate(st_provider, dataset, st_ids)
        mrr_lift = round(after["mrr"] - before["mrr"], 4)
        pct = round(100.0 * mrr_lift / before["mrr"], 2) if before["mrr"] else 0.0
        report["self_tuning"] = {
            "before": before,
            "after": after,
            "mrr_lift": mrr_lift,
            "mrr_lift_pct": pct,
        }

    md = _markdown(report)
    print(md)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n[wrote JSON: {args.json}]", file=sys.stderr)
    if args.markdown:
        Path(args.markdown).write_text(md, encoding="utf-8")
        print(f"[wrote markdown: {args.markdown}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
