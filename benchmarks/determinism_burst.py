"""Separate TRUE nondeterminism from clock drift, across the WHOLE query set.

WHY THIS EXISTS. A recency-weighted store recomputes decay at query time, so
every score falls with the wall clock — measured at ~4e-8 per second, strictly
monotonic. Records decay at DIFFERENT rates (~4.5e-8 to 6.1e-8/s, a 34%
spread), so near-tied candidates cross each other. In a corpus with a
degenerate band the gap is ~3e-7, which crosses in about twenty seconds.

That means the obvious determinism test — open the database N times minutes
apart and compare — measures a property a recency-weighted engine cannot have.
Two runs minutes apart SHOULD differ.

THE METHOD. Do the slow work first — copy every database AND open every engine
— then query them all back-to-back. The queries take milliseconds, so total
drift across the burst is ~1e-8, far below any plausible crossing. Ordering
differences inside that window cannot be time.

WHY IT SWEEPS EVERY QUERY, which is the part that was learned the hard way.
An earlier version of this file probed ONE query ("What is Taylor's role?") and
reported "stable in both arms 5/5". That verdict was true of the query and
false of the build: `What is Jack's role?` varied across instances in 2 of 8
bursts on the same engine. A green light was published on a build that was not
green, because the instrument's window was one query wide and the claim was a
whole build wide.

The rule that failure earns: THE INSTRUMENT'S WINDOW IS PART OF THE CLAIM. A
determinism probe must sweep the query set it is certifying, and it must name
which queries it swept. So this reports every varying query by name, and any
single varying query fails the whole run.

TWO ARMS, because they attribute differently:

  A  one instance, N consecutive queries   -> per-QUERY nondeterminism
  B  N instances, one query each           -> per-INSTANCE (open-time) state

Arm A carries a confound worth knowing: `recall_text` has no `skip_reinforce`,
so repeated queries mutate `access_count`, and query N sees a database queries
1..N-1 modified. Treat a small arm-A signal with suspicion; arm B is clean.

Intermittency matters: a source that fires in 2 of 8 bursts is invisible to a
single run. Use --bursts to repeat, and read "bursts_with_variation".

    python benchmarks/determinism_burst.py <seeded.db> [instances] [--bursts N]
"""

from __future__ import annotations

import argparse
import gc
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

DECAY_PER_SECOND = 5e-8
CROSSING_SECONDS = 20
# Measured gap between near-tied candidates in this corpus's degenerate band.
# The window is "tight enough" when the drift inside it is small COMPARED TO
# THIS — not compared to some fraction of a clock. An arbitrary time threshold
# would answer a question nobody asked; the real question is whether decay
# could have reordered a tie during the comparison.
TIE_GAP = 3e-7

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "comparison" / "fixtures"
ROLE_NAMES = ("Taylor", "Jack", "Dave", "Bob")


def _queries() -> list[str]:
    """Every query this gate certifies — role queries AND the possessive pairs.

    Named explicitly so a reader can see the window the verdict covers.
    """
    qs = [f"What is {n}'s role?" for n in ROLE_NAMES]
    probes = json.loads(
        (FIXTURES / "direction_probes_v1_1.json").read_text(encoding="utf-8"))["probes"]
    for p in probes:
        qs.append(p["query_possessive"])
        qs.append(p["query_subject"])
    return qs


def _stage(base: Path, into: Path) -> Path:
    for f in base.parent.glob(base.name + "*"):
        shutil.copy2(f, into / f.name)
    return into / base.name


def _burst(YantrikDB, base: Path, n: int, queries: list[str]) -> dict:
    root = Path(tempfile.mkdtemp())
    try:
        d = root / "single"
        d.mkdir()
        single = YantrikDB.with_default(str(_stage(base, d)))

        dbs = []
        for i in range(n):
            dd = root / f"inst{i}"
            dd.mkdir()
            dbs.append(YantrikDB.with_default(str(_stage(base, dd))))

        def order(db, q):
            return tuple((h.get("text") or "").strip()[:24]
                         for h in db.recall_text(q, top_k=5, namespace="g"))

        t0 = time.time()
        arm_a = {q: len({order(single, q) for _ in range(n)}) for q in queries}
        span_a = time.time() - t0

        # Per-query window is the denominator that matters: each query's
        # cross-instance comparison happens inside ITS OWN window, not the
        # whole arm's. Using the arm span would flag a tight comparison as
        # untrustworthy — the wrong denominator for the claim being made.
        t1 = time.time()
        arm_b, per_query_windows = {}, []
        for q in queries:
            qt = time.time()
            arm_b[q] = len({order(db, q) for db in dbs})
            per_query_windows.append(time.time() - qt)
        span_b = time.time() - t1

        del single, dbs
        gc.collect()
        return {"arm_a": arm_a, "arm_b": arm_b,
                "span_a": span_a, "span_b": span_b,
                "max_per_query_window": max(per_query_windows)}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db")
    ap.add_argument("instances", nargs="?", type=int, default=6)
    ap.add_argument("--bursts", type=int, default=1,
                    help="repeat; a source firing 2-in-8 is invisible to one run")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    base = Path(args.db)
    queries = _queries()

    varied_b: dict[str, int] = {}
    varied_a: dict[str, int] = {}
    bursts_with_variation = 0
    spans_b, windows = [], []

    for _ in range(args.bursts):
        r = _burst(YantrikDB, base, args.instances, queries)
        spans_b.append(r["span_b"])
        windows.append(r["max_per_query_window"])
        bad_b = [q for q, c in r["arm_b"].items() if c > 1]
        bad_a = [q for q, c in r["arm_a"].items() if c > 1]
        for q in bad_b:
            varied_b[q] = varied_b.get(q, 0) + 1
        for q in bad_a:
            varied_a[q] = varied_a.get(q, 0) + 1
        if bad_b or bad_a:
            bursts_with_variation += 1

    span = max(spans_b)
    window = max(windows)
    clean = not varied_a and not varied_b
    print(json.dumps({
        "queries_swept": len(queries),
        "instances": args.instances,
        "bursts": args.bursts,
        "bursts_with_variation": bursts_with_variation,
        "max_arm_b_span_s": round(span, 3),
        "max_per_query_comparison_window_s": round(window, 4),
        "drift_in_comparison_window": f"{window * DECAY_PER_SECOND:.2e}",
        "tie_gap_in_band": f"{TIE_GAP:.1e}",
        "drift_as_fraction_of_tie_gap": round(window * DECAY_PER_SECOND / TIE_GAP, 3),
        "window_tight_enough": window * DECAY_PER_SECOND < 0.1 * TIE_GAP,
        "why_this_denominator": (
            "each query is compared across instances inside its OWN window; "
            "the whole-arm span is irrelevant to that comparison and using it "
            "would understate how tight the measurement actually is"),
        "queries_varying_across_instances": varied_b,
        "queries_varying_within_instance": varied_a,
        "verdict": (
            "DETERMINISTIC across every swept query" if clean else
            "NOT DETERMINISTIC — see queries_varying_*; a single varying query "
            "fails the run, because a verdict may not be broader than the "
            "window that produced it"),
        "note": ("differences across MINUTES are expected and are not a defect:"
                 " recency decay is recomputed per query by design"),
    }, indent=2))
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
