"""Idle-CPU benchmark for the embedded engine — the shared instrument.

Background. A Hermes user running ~6 agents on a Ryzen 9950X3D reported the
backend burning ~55% of a 32-logical-processor machine *while idle*. Cause:
the engine's materializer polled every 100 ms for unapplied operations using
an index that was declared only in a schema migration and never in the base
schema — so every database *created* after that migration lacked it, and each
poll fell back to walking the whole oplog. Idle cost therefore grew with
history depth. Fixed in engine issue #113; this harness is what measured it,
and what keeps it from coming back.

Two lessons are baked into the code rather than written down, because a
protocol that lives in prose does not execute:

1. QUIESCE ON STATE, NOT ON TIME. A fixed sleep before sampling silently
   measures the materializer *draining* and reports it as idle. Correct
   settle time is a function of machine speed and burst size, so any constant
   is wrong on a slow box or a big corpus. We block until
   ``oplog WHERE applied = 0`` reads zero, then sample. Measuring drain and
   calling it idle produced a 12.4%-of-machine reading that was pure
   artifact — and, in another run, made 6 engines look 15x cheaper than 1.

2. STAMP THE WORLD. Every result carries the engine version, schema version,
   and host CPU count. A measurement is an implicit claim about the system it
   was taken on, and that claim is invisible in the number: the original
   "31.9% -> 4.5% of machine" figure for the plugin's engine cache stayed
   reproducible while quietly becoming a statement about an engine revision
   nobody would run again.

Usage:
    python benchmarks/idle_cpu_bench.py curve   [--engines N] [--steps A,B,C]
    python benchmarks/idle_cpu_bench.py compare [--engines N] [--records N]
    python benchmarks/idle_cpu_bench.py gate    [--records N]

    curve   — idle CPU vs history depth. The regression *shape*: with worker
              count held constant this must stay flat.
    compare — 1 engine vs N engines on identical history. Sizes what sharing
              one engine per database is worth.
    gate    — the committed thresholds:
                A (scaling invariant) cpu(10k) <= 1.25 x cpu(1k)
                B (absolute)          < 5% of one core, median
              Gate A is the one that matters: it tests shape, so it is
              hardware-independent and catches the defect returning even if
              constants change. Pre-fix its ratio was ~27; post-fix ~1.0.

Requires ``psutil`` and an installed ``yantrikdb`` engine. Run from a
directory other than the repo root, or let _bootstrap pin the import — the
plugin package shares the engine's name (see issue #50).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import tempfile
import time

try:
    import psutil
except ImportError:  # pragma: no cover - operator-facing message
    sys.exit("idle_cpu_bench needs psutil: pip install psutil")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _bootstrap import _pin_engine_import  # noqa: E402

_pin_engine_import()

from yantrikdb._yantrikdb_rust import YantrikDB  # noqa: E402

import yantrikdb  # noqa: E402

SAMPLE_SECONDS = 6.0
SAMPLES = 3
DRAIN_TIMEOUT = 900.0
NCPU = os.cpu_count() or 1


# --------------------------------------------------------------------------
# Stamping — a number without its world is not evidence
# --------------------------------------------------------------------------

def _schema_version(db_path: str) -> str:
    for query in ("SELECT value FROM meta WHERE key = 'schema_version'",
                  "PRAGMA user_version"):
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = con.execute(query).fetchone()
            con.close()
            if row and str(row[0]) not in ("", "0"):
                return str(row[0])
        except sqlite3.Error:
            continue
    return "?"


def stamp(db_path: str) -> str:
    return (f"engine={getattr(yantrikdb, '__version__', '?')} "
            f"schema={_schema_version(db_path)} cpus={NCPU}")


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def pending_ops(db_path: str) -> int:
    """Operations the materializer still has to apply. -1 if unreadable."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        n = con.execute("SELECT count(*) FROM oplog WHERE applied = 0").fetchone()[0]
        con.close()
        return int(n)
    except sqlite3.Error:
        return -1


def wait_for_drain(db_path: str, timeout: float = DRAIN_TIMEOUT) -> bool:
    """Block until the materializer has nothing left to apply.

    This is the quiesce. Do not replace it with a sleep — see module docstring.
    Requires three consecutive zero reads so a momentary dip mid-drain cannot
    be mistaken for completion, then a short settle for post-drain work.
    """
    deadline = time.time() + timeout
    zeros = 0
    while time.time() < deadline:
        n = pending_ops(db_path)
        if n == 0:
            zeros += 1
            if zeros >= 3:
                time.sleep(5.0)
                return True
        elif n < 0:
            return True          # cannot observe; fall through rather than hang
        else:
            zeros = 0
        time.sleep(2.0)
    print("  WARNING: drain did not finish; the sample below includes real work",
          file=sys.stderr)
    return False


def idle_cpu(db_path: str) -> float:
    """Median idle CPU as a percentage of ONE core, after full drain.

    Median, not mean: post-drain peaks reach ~4x the median, so a max- or
    mean-based reading is noise-dominated and a gate built on one is flaky.
    """
    wait_for_drain(db_path)
    proc = psutil.Process()
    samples = []
    for _ in range(SAMPLES):
        proc.cpu_percent(None)
        time.sleep(SAMPLE_SECONDS)
        samples.append(proc.cpu_percent(None))
    return statistics.median(samples)


def seed(engine, upto: int, written: int) -> tuple[int, int]:
    """Write records until the corpus holds ``upto``. Returns (written, backpressure)."""
    backpressure = 0
    while written < upto:
        try:
            engine.record_text(
                f"probe fact {written}: the quick brown fox jumps over the lazy dog",
                memory_type="semantic")
            written += 1
        except Exception as exc:  # noqa: BLE001 — engine typed exc varies by version
            if "queue full" in str(exc) or "Backpressure" in type(exc).__name__:
                backpressure += 1
                time.sleep(0.05)
                continue
            raise
    return written, backpressure


def report(label: str, cpu: float, threads: int) -> None:
    print(f"  {label:<34} threads={threads:4d}  cpu={cpu:7.1f}% core  "
          f"({cpu / NCPU:5.2f}% machine)", flush=True)


def open_engines(db_path: str, n: int) -> list:
    return [YantrikDB.with_default(db_path) for _ in range(n)]


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def cmd_curve(args) -> int:
    db_path = os.path.join(tempfile.mkdtemp(), "bench.db")
    engines = open_engines(db_path, args.engines)
    proc = psutil.Process()
    print(f"curve | {args.engines} engines | {stamp(db_path)}", flush=True)
    written = total_bp = 0
    results = []
    for step in args.steps:
        written, bp = seed(engines[0], step, written)
        total_bp += bp
        cpu = idle_cpu(db_path)
        results.append((step, cpu))
        report(f"idle @ {step} records", cpu, proc.num_threads())
    print(f"  backpressure events while seeding: {total_bp}", flush=True)
    first, last = results[0][1], results[-1][1]
    if first > 0:
        print(f"\n  shape: cpu({results[-1][0]}) / cpu({results[0][0]}) = "
              f"{last / first:.2f}x  (flat => idle cost independent of history)")
    return 0


def cmd_compare(args) -> int:
    print(f"compare | {args.records} records | 1 vs {args.engines} engines", flush=True)
    proc = psutil.Process()
    measured = {}
    for count in (1, args.engines):
        db_path = os.path.join(tempfile.mkdtemp(), "bench.db")
        engines = open_engines(db_path, count)
        seed(engines[0], args.records, 0)
        cpu = idle_cpu(db_path)
        measured[count] = cpu
        report(f"{count} engine(s)", cpu, proc.num_threads())
        del engines
    if measured[1] > 0:
        print(f"\n  {args.engines} engines cost {measured[args.engines] / measured[1]:.2f}x "
              f"the idle CPU of 1 shared engine")
    return 0


def cmd_gate(args) -> int:
    """Committed thresholds. Exit non-zero on failure so CI can consume it."""
    db_path = os.path.join(tempfile.mkdtemp(), "bench.db")
    open_engines(db_path, 1)
    engine = YantrikDB.with_default(db_path)
    print(f"gate | 1 engine | {stamp(db_path)}", flush=True)

    written, _ = seed(engine, 1000, 0)
    small = idle_cpu(db_path)
    report("idle @ 1,000 records", small, psutil.Process().num_threads())

    written, _ = seed(engine, args.records, written)
    large = idle_cpu(db_path)
    report(f"idle @ {args.records:,} records", large, psutil.Process().num_threads())

    ratio = (large / small) if small > 0 else 0.0
    gate_a = ratio <= 1.25
    gate_b = large < 5.0
    print(f"\n  Gate A  scaling invariant  ratio={ratio:5.2f} <= 1.25   "
          f"{'PASS' if gate_a else 'FAIL'}")
    print(f"  Gate B  absolute ceiling   {large:5.2f}% core  < 5.00%   "
          f"{'PASS' if gate_b else 'FAIL'}")
    if not (gate_a and gate_b):
        print("\n  Idle cost is scaling with history depth again — check that the "
              "pending-oplog index exists ON A FRESHLY CREATED database (engine "
              "#113 was a migration-only index, absent from the base schema) and "
              "that EXPLAIN QUERY PLAN uses it for the drain query.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("curve", help="idle CPU vs history depth")
    p.add_argument("--engines", type=int, default=6)
    p.add_argument("--steps", type=lambda s: [int(x) for x in s.split(",")],
                   default=[500, 2000, 6000])
    p.set_defaults(func=cmd_curve)

    p = sub.add_parser("compare", help="1 engine vs N engines, same history")
    p.add_argument("--engines", type=int, default=6)
    p.add_argument("--records", type=int, default=6000)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("gate", help="committed idle-CPU thresholds")
    p.add_argument("--records", type=int, default=10000)
    p.set_defaults(func=cmd_gate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
