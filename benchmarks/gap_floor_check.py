"""Convict your own copy of the composite-threshold bug, in one command.

A threshold compared against a COMPOSITE recall score inherits an unstated
assumption: that the composite discriminates "memory has something near this"
from "memory has nothing." It does not have to. The composite folds in terms —
importance, recency, decay — that describe how much a result should be weighted
ONCE you've decided to return it, not whether it should have been returned. On
corpora where those terms carry little variance, composite scores cluster high
and a threshold set for a "low score" is never crossed. The detector doesn't
misfire; it goes silent, and silence reads as good news.

This measures the actual distribution on YOUR database and probes it with
deliberate nonsense, which is the case such a detector exists for.

    python gap_floor_check.py <db_path> [--namespace NS] [--threshold 0.30]

Exit 1 if nonsense queries score above your threshold, so it can gate CI.

NOTE ON METHOD: this reports OBSERVED distributions only. It does not compute a
lower bound on the composite, because the returned per-component contributions
do NOT sum to the composite score — verified: they differ by ~0.2, and a
composite can land below the sum of its own non-similarity contributions. Any
tool claiming a derived "floor" from those contributions is measuring something
that isn't there. Ask what the detector DID, not what the arithmetic suggests
it must do.

Uses the direct binding because it needs the `scores` breakdown; over MCP the
composite is visible but its composition is not, which is one reason this can
hide in production.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys

# Deliberate nonsense. A working detector flags all of these. Any that score
# above the threshold are being reported as confidently answered.
NONSENSE = [
    "zzqx wobble frangible",
    "quantum tarpaulin metric",
    "grommet fitzwilliam parsnip protocol",
    "vermillion sprocket abeyance clause",
]

# Ordinary queries, used only to sample the score distribution on real content.
PROBES = [
    "what did the user decide",
    "recent architecture choice",
    "what is the current configuration",
    "who is responsible for this",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db_path")
    ap.add_argument("--namespace", default=None)
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="the value you calibrated against the composite score")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    from yantrikdb._yantrikdb_rust import YantrikDB
    db = YantrikDB.with_default(args.db_path)

    def recall(q):
        kw = {"top_k": args.top_k}
        if args.namespace:
            kw["namespace"] = args.namespace
        return db.recall(q, **kw)

    def sample(queries):
        out = []
        for q in queries:
            hits = recall(q)
            if not hits:
                continue
            out.append({
                "query": q,
                "avg": statistics.mean(h["score"] for h in hits),
                "min": min(h["score"] for h in hits),
                "max_similarity": max(
                    (h.get("scores") or {}).get("similarity", 0.0) for h in hits),
            })
        return out

    real, junk = sample(PROBES), sample(NONSENSE)
    if not real and not junk:
        print("No results for any probe — nothing to measure.", file=sys.stderr)
        return 2

    every = real + junk
    evaded = [r for r in junk if r["avg"] >= args.threshold]
    real_flagged = [r for r in real if r["avg"] < args.threshold]

    print(json.dumps({
        "threshold_under_test": args.threshold,
        "observed_avg_score": {
            "min": round(min(r["avg"] for r in every), 4),
            "max": round(max(r["avg"] for r in every), 4)},
        "lowest_single_result_score": round(min(r["min"] for r in every), 4),
        "max_similarity_seen": {
            "real_queries": round(max((r["max_similarity"] for r in real), default=0), 4),
            "nonsense_queries": round(max((r["max_similarity"] for r in junk), default=0), 4)},
        "nonsense_avg_scores": {r["query"]: round(r["avg"], 4) for r in junk},
        "nonsense_queries_NOT_flagged": f"{len(evaded)}/{len(junk)}",
        "real_queries_flagged_as_gap": f"{len(real_flagged)}/{len(real)}",
        "verdict": (
            "BROKEN — nonsense scores above your threshold and is reported as "
            "answered; this detector's silence carries no information"
            if evaded else "threshold discriminates nonsense on this corpus"),
        "similarity_separates_nonsense": (
            max((r["max_similarity"] for r in real), default=0)
            > max((r["max_similarity"] for r in junk), default=0)),
        "read_this_before_switching_to_similarity": (
            "Compare the two max_similarity figures. If nonsense scores as high "
            "as real queries, thresholding similarity will NOT fix the detector "
            "either — the embedder is assigning arbitrary strings real "
            "neighbours, and no scalar cut over that signal can separate them. "
            "Measure before adopting; do not assume similarity is the answer "
            "just because the composite is not."),
    }, indent=2))

    if evaded:
        print(f"\n{len(evaded)} nonsense quer{'y' if len(evaded)==1 else 'ies'} "
              f"would be reported as answered:", file=sys.stderr)
        for r in evaded:
            print(f"  {r['query']!r}  avg={r['avg']:.4f}", file=sys.stderr)
    return 1 if evaded else 0


if __name__ == "__main__":
    raise SystemExit(main())
