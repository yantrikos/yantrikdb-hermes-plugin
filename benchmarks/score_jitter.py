"""Characterise score variation across opens, before choosing a quantum.

WHY MEASURE FIRST. When scores wobble between opens, the tempting fix is to
quantize comparisons so the wobble stops mattering. That only works if the
quantum sits comfortably ABOVE the wobble and below any meaningful difference.
A quantum chosen at the same scale as the jitter fixes nothing — values land on
opposite sides of a bucket boundary just as often as before.

A real engine fix quantized at 1e-6 while the jitter WAS 1e-6, and the
instability survived. Measuring the distribution first is one command.

WHAT THE SHAPE TELLS YOU. Wobble that rises monotonically across sequential
opens is the wall clock — recency decay recomputed per query, by design, not a
defect. Wobble that scatters is a genuine ordering or summation difference.
This reports both the magnitude and the per-query breakdown so the two are
distinguishable; use `determinism_burst.py` to settle it drift-free.

Reports membership churn alongside, because a score that "varies" for a record
present in only some opens is a different fact from one that varies for a
record present in all of them.

    python benchmarks/score_jitter.py <seeded.db> [top_k]
"""

from __future__ import annotations

import gc
import json
import shutil
import sys
import tempfile
from pathlib import Path

QUERIES = (
    "What is Taylor's role?",
    "What is Bob's role?",
    "taylor",
    "What is Dave's role?",
    "who reports to Grace",
)
OPENS = 5


def _stage(base: Path, into: Path) -> Path:
    for f in base.parent.glob(base.name + "*"):
        shutil.copy2(f, into / f.name)
    return into / base.name


def main() -> int:
    base = Path(sys.argv[1])
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    root = Path(tempfile.mkdtemp())
    out, worst = {}, 0.0
    try:
        for qi, q in enumerate(QUERIES):
            runs = []
            for i in range(OPENS):
                d = root / f"q{qi}_{i}"
                d.mkdir(parents=True)
                db = YantrikDB.with_default(str(_stage(base, d)))
                runs.append({(h.get("text") or "").strip(): h.get("score", 0.0)
                             for h in db.recall_text(q, top_k=top_k, namespace="g")})
                del db
                gc.collect()
            sets_ = [set(r) for r in runs]
            common, union = set.intersection(*sets_), set.union(*sets_)
            deltas = sorted(max(r[t] for r in runs) - min(r[t] for r in runs)
                            for t in common)
            worst = max(worst, deltas[-1] if deltas else 0.0)
            out[q] = {
                "returned": len(union),
                "stable_membership": len(common),
                "churned": len(union - common),
                "max_jitter": f"{deltas[-1]:.3e}" if deltas else "0",
                "median_jitter": f"{deltas[len(deltas) // 2]:.3e}" if deltas else "0",
                "count_above_1e-5": sum(1 for d in deltas if d > 1e-5),
            }
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(json.dumps({
        "top_k": top_k, "opens_per_query": OPENS, "by_query": out,
        "recommended_quantum": f"{worst * 10:.1e}",
        "quantum_rule": ("a quantum must exceed the observed jitter by an order "
                         "of magnitude; one chosen at the jitter's own scale "
                         "leaves values straddling bucket boundaries and fixes "
                         "nothing"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
