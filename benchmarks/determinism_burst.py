"""Separate TRUE nondeterminism from clock drift, without an engine API for it.

WHY THIS EXISTS. A recency-weighted store recomputes decay at query time, so
every score falls with the wall clock — measured at ~4e-8 per second, strictly
monotonic. Records decay at DIFFERENT rates (~4.5e-8 to 6.1e-8/s, a 34%
spread), so near-tied candidates cross each other. In a corpus with a
degenerate band the gap is ~3e-7, which crosses in about twenty seconds.

That means the obvious determinism test — open the database N times and compare
answers — measures a property a recency-weighted engine cannot have. Two runs
minutes apart SHOULD differ. Reading that as a bug wasted most of a debugging
session, in both directions: first concluding the engine was nondeterministic
when it was drifting, then concluding it was only drifting when real
nondeterminism remained underneath.

THE METHOD. Do the slow work first — copy every database AND open every engine
— then query them all back-to-back. The queries take milliseconds, so the total
drift across the burst is ~1e-8, far below any plausible crossing. Ordering
differences inside that window cannot be time.

TWO ARMS, because they attribute differently:

  A  one instance, N consecutive queries   -> per-QUERY nondeterminism
  B  N instances, one query each           -> per-INSTANCE (open-time) state

Arm A carries a confound worth knowing: `recall_text` has no `skip_reinforce`,
so repeated queries mutate `access_count`, and query N sees a database queries
1..N-1 modified. Treat a small arm-A signal with suspicion; arm B is clean.

The burst span and its drift budget are always reported, because a burst that
isn't short enough proves nothing and the reader must be able to check.

    python benchmarks/determinism_burst.py <seeded.db> [instances]
"""

from __future__ import annotations

import gc
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Measured on this engine: score decay per second of wall clock.
DECAY_PER_SECOND = 5e-8
# Approximate time for near-tied candidates in a degenerate band to cross.
CROSSING_SECONDS = 20
QUERY = "What is Taylor's role?"


def _stage(base: Path, into: Path) -> Path:
    for f in base.parent.glob(base.name + "*"):
        shutil.copy2(f, into / f.name)
    return into / base.name


def main() -> int:
    base = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 6

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    root = Path(tempfile.mkdtemp())

    def order(db) -> tuple:
        return tuple((h.get("text") or "").strip()[:24]
                     for h in db.recall_text(QUERY, top_k=5, namespace="g"))

    # Arm A — one instance, repeated queries.
    d = root / "single"
    d.mkdir()
    single = YantrikDB.with_default(str(_stage(base, d)))
    t0 = time.time()
    a = [order(single) for _ in range(n)]
    span_a = time.time() - t0

    # Arm B — N instances, one query each. Stage and OPEN everything first so
    # the burst contains only queries.
    dbs = []
    for i in range(n):
        dd = root / f"inst{i}"
        dd.mkdir()
        dbs.append(YantrikDB.with_default(str(_stage(base, dd))))
    t1 = time.time()
    b = [order(db) for db in dbs]
    span_b = time.time() - t1

    drift_b = span_b * DECAY_PER_SECOND
    per_instance = len(set(b)) > 1
    per_query = len(set(a)) > 1

    print(json.dumps({
        "instances": n,
        "arm_A_same_instance": {
            "span_s": round(span_a, 4), "distinct_orderings": len(set(a)),
            "caveat": "repeated queries reinforce access_count; weak signal here"
                      " may be state, not nondeterminism",
        },
        "arm_B_distinct_instances": {
            "span_s": round(span_b, 4), "distinct_orderings": len(set(b)),
            "drift_across_burst": f"{drift_b:.2e}",
            "burst_short_enough": span_b < CROSSING_SECONDS / 10,
        },
        "verdict": (
            "TRUE nondeterminism in open-time state — instances of the same "
            "file disagree inside a drift-free window" if per_instance else
            "per-query nondeterminism" if per_query else
            "stable in both arms"),
        "note": ("differences across MINUTES are expected and are not a defect:"
                 " recency decay is recomputed per query by design"),
    }, indent=2))

    del single, dbs
    gc.collect()
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
