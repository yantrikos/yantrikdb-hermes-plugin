"""Localise a ranking instability by widening the selection window.

THE TECHNIQUE. Run the same query at increasing `top_k` across repeated opens
and watch where the instability disappears. If results churn at k=5 and are
identical at k=60, the defect is not in candidate generation or scoring — it is
in whatever closes the window. That converts "somewhere in retrieval" into a
stage attribution in one sweep, without reading any engine source.

It found this on a real engine candidate:

    k     distinct orderings   returned   churned
    5            3                10         10      <- 200% of k
    10           3                15         10
    15           2                16          2
    25           1                25          0      <- clean
    60           1                60          0

Churn scaling inversely with the window is the signature of a greedy selector
whose early picks dominate: with five slots each choice is decisive and one
marginal difference cascades; with sixty almost everything is admitted and the
path cannot diverge.

A CAVEAT THIS TOOL CANNOT FIX. Sequential opens span seconds, and recency decay
is recomputed per query, so some churn here is the clock rather than the
engine. Small residual churn scattered K-INDEPENDENTLY is drift; churn that
spikes at small k and vanishes at large k is the selector. Use
`determinism_burst.py` when you need a drift-free verdict — this tool is for
localisation, not certification.

    python benchmarks/ksweep.py <seeded.db> [query]
"""

from __future__ import annotations

import gc
import json
import shutil
import sys
import tempfile
from pathlib import Path

KS = (5, 8, 10, 12, 15, 20, 25, 30, 40, 60)
OPENS = 5
DEFAULT_QUERY = "What is Taylor's role?"


def _stage(base: Path, into: Path) -> Path:
    for f in base.parent.glob(base.name + "*"):
        shutil.copy2(f, into / f.name)
    return into / base.name


def main() -> int:
    base = Path(sys.argv[1])
    query = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_QUERY

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    root = Path(tempfile.mkdtemp())
    out = {}
    try:
        for ki, k in enumerate(KS):
            orders, sets_ = [], []
            for i in range(OPENS):
                d = root / f"k{ki}_{i}"
                d.mkdir(parents=True)
                db = YantrikDB.with_default(str(_stage(base, d)))
                res = [(h.get("text") or "").strip()
                       for h in db.recall_text(query, top_k=k, namespace="g")]
                orders.append(tuple(res))
                sets_.append(set(res))
                del db
                gc.collect()
            common = set.intersection(*sets_)
            union = set.union(*sets_)
            out[k] = {"distinct_orderings": len(set(orders)),
                      "returned": len(union),
                      "churned": len(union - common)}
    finally:
        shutil.rmtree(root, ignore_errors=True)

    small = out[KS[0]]["churned"]
    large = out[KS[-1]]["churned"]
    print(json.dumps({
        "query": query, "opens_per_k": OPENS, "by_k": out,
        "reading": (
            "churn concentrated at small k — points at the selection window, "
            "not at candidate generation or scoring"
            if small > large else
            "churn is not k-dependent — more consistent with recency drift "
            "than with a selector; confirm with determinism_burst.py"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
