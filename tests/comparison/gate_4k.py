"""The competing-distractor gate — the one both sides run against a candidate build.

This is a STRESS corpus, deliberately pathological, and that is the point. Its
distractors come from the same generators as its answers, so ~3,000 records
share five sentence frames and differ by a single entity token. Two properties
follow, and they are what the gate exists to measure:

  1. BM25 goes degenerate. 143 records match `taylor`; they carry 4 distinct
     bm25 values. Every matcher gets lex = 1.0, so a boost that is monotone in
     lex is constant across the matched set and cannot reorder within it.
  2. Cosine loses relation DIRECTION. Under potion-2M, "Pat reports to Taylor"
     scores 0.749 against the query `taylor` while "Taylor reports to Carol"
     scores 0.439 — subject position ranks below object position.

Real memory is less uniform than this, so treat a regression here as a signal
about mechanism, not a forecast of field precision. Weight a production-clone
gate above this one for any user-facing claim.

WHAT v1.2.0 CHANGED, AND WHY — three defects found by running this gate against
real candidate builds, all of which produced confident numbers that meant
nothing:

  AMBIGUOUS GROUND TRUTH. The `What is X's role?` queries have NINETEEN valid
  answers each — X reports to nineteen different people in this corpus. Scoring
  them by whether one arbitrarily-designated record appears caps precision at
  5/19 by construction, and *punishes* a mechanism that correctly promotes
  other valid answers. Those queries are now scored by ROLE SHARE, and a
  startup assertion refuses to score any query by record identity unless its
  answer is unique in the corpus. A gate that silently measures the wrong thing
  is worse than no gate.

  STATE MUTATION MISREAD AS NOISE. `recall_text` has no `skip_reinforce`, so
  every query mutates `access_count`, which feeds scoring. Repeated measurement
  therefore measures a database its own earlier repeats modified. Each repeat
  now runs against a fresh COPY of the seeded database.

  A METRIC THAT AMPLIFIED NOISE. Set overlap at top_k=5 converts one rank swap
  into a 0.2 swing; on identical isolated runs it showed stdev 0.048 where a
  rank-based statistic showed 0.000. The possessive axis now leads with top-1
  agreement, and Jaccard is retained only as a secondary reading.

Corpus and queries are hash-pinned so iterations stay comparable. If a hash
check fails the gate refuses to run rather than quietly reporting numbers from
a different corpus. NOTE: v1.2.0 changes only SCORING — the fixtures and their
hashes are unchanged from v1.1.0.

WHAT v1.4.0 ADDS. A comparison now carries its proof of identity: all three
fixture hashes, the complete seed bundle hash before its first database open,
seed age for every metric window, import provenance, raw metric values, and
cold-open ordering signatures with observation times. The companion comparator
runs both interpreters in alternating order against those exact seed bytes and
refuses mismatched reports before calculating a delta.

Run:
    python tests/comparison/gate_4k.py --repeats 7 --direction
    python tests/comparison/gate_4k.py --db <seeded.db> --repeats 7
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from importlib import metadata as importlib_metadata
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus_4353_gate_v1.json"
QUERIES = FIXTURES / "queries_1k.json"
PROBES = FIXTURES / "direction_probes_v1_1.json"

GATE_NAME = "hermes-plugin/competing-distractors"
GATE_VERSION = "1.4.0"

# Pinned at gate v1.0.0 and unchanged since. A candidate build must be measured
# against the same bytes the previous candidate was measured against.
CORPUS_SHA256 = "2d2d039094644ce5b2a1d8de5047daa5b4a183e98919bdae3a57a67962df4fc9"
QUERIES_SHA256 = "a4b866a5bdeccfb103b25d2cf1a66a1ca50af9096ccb55bf73579f676f4b407f"
PROBES_SHA256 = "dfe0242e72fa575c5d45bf7afc8e2e153dde279fc45be2b049da15f6e0383a3d"
FIXTURE_HASHES = {
    "corpus_sha256": CORPUS_SHA256,
    "queries_sha256": QUERIES_SHA256,
    "probes_sha256": PROBES_SHA256,
}

# Seeding is async; measuring before the write queue drains reports compaction
# noise as retrieval quality.
DRAIN_SECONDS = 30
TOP_K = 5
STABILITY_QUERY = "What is Taylor's role?"

_REPORTS_TO = re.compile(r"^([A-Z][a-z]+) reports to ([A-Z][a-z]+)\.$")


def _load() -> tuple[list[dict], list[dict], list[dict]]:
    blobs = {
        CORPUS.name: CORPUS.read_text(encoding="utf-8"),
        QUERIES.name: QUERIES.read_text(encoding="utf-8"),
        PROBES.name: PROBES.read_text(encoding="utf-8"),
    }
    for name, blob in blobs.items():
        want = {
            CORPUS.name: CORPUS_SHA256,
            QUERIES.name: QUERIES_SHA256,
            PROBES.name: PROBES_SHA256,
        }[name]
        got = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        if got != want:
            raise SystemExit(
                f"{name} hash mismatch — this is not the pinned gate corpus.\n"
                f"  expected {want}\n  actual   {got}\n"
                "Numbers from a drifted corpus are not comparable to prior runs."
            )
    return (
        json.loads(blobs[CORPUS.name])["facts"],
        json.loads(blobs[QUERIES.name])["queries"],
        json.loads(blobs[PROBES.name])["probes"],
    )


def _seed_bytes(base: Path) -> dict:
    """Hash the complete database bundle without opening it."""
    files = sorted(p for p in base.parent.glob(base.name + "*") if p.is_file())
    bundle = hashlib.sha256()
    entries = []
    for path in files:
        blob = path.read_bytes()
        bundle.update(path.name.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(blob)
        stat = path.stat()
        entries.append({
            "name": path.name,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
            "mtime_ns": stat.st_mtime_ns,
        })
    return {
        "sha256": bundle.hexdigest(),
        "bytes": sum(f["bytes"] for f in entries),
        "mtime_unix": max(f["mtime_ns"] for f in entries) / 1e9,
        "latest_mtime_ns": max(f["mtime_ns"] for f in entries),
        "files": entries,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_snapshot(base: Path) -> dict:
    """Content identity and clock origin for the immutable seed bundle."""
    if not base.is_file():
        raise SystemExit(f"seed database does not exist: {base}")

    # This digest is deliberately BEFORE the first SQLite open. Even a
    # metadata-only connection can touch WAL/SHM state; a report that hashes
    # afterward cannot prove both candidate processes started from the bytes
    # the comparator supplied.
    snapshot = _seed_bytes(base)
    uri = f"file:{base.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        created_min, created_max, record_count = con.execute(
            "SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM memories"
        ).fetchone()
    finally:
        con.close()
    if _seed_bytes(base)["sha256"] != snapshot["sha256"]:
        raise SystemExit(
            "seed hash changed during metadata read — use a stable, checkpointed seed")
    return {
        "path": str(base.resolve()),
        **snapshot,
        "created_at_min": created_min,
        "created_at_max": created_max,
        "record_count": record_count,
    }


def _import_metadata(yantrikdb) -> dict:
    """Prove which interpreter, package, and native extension produced a run."""
    from yantrikdb import _yantrikdb_rust

    module_path = Path(yantrikdb.__file__).resolve()
    extension_path = Path(_yantrikdb_rust.__file__).resolve()
    dist = importlib_metadata.distribution("yantrikdb")
    direct_url = {}
    if raw := dist.read_text("direct_url.json"):
        try:
            direct_url = json.loads(raw)
        except json.JSONDecodeError:
            pass
    editable = bool(direct_url.get("dir_info", {}).get("editable"))
    import_source = (
        "editable" if editable else
        "site-packages" if "site-packages" in {p.lower() for p in module_path.parts} else
        "other"
    )
    archive_hash = direct_url.get("archive_info", {}).get("hash")
    wheel_sha256 = archive_hash.removeprefix("sha256=") if (
        isinstance(archive_hash, str) and archive_hash.startswith("sha256=")) else None
    return {
        "distribution_version": dist.version,
        "module_file": str(module_path),
        "import_source": import_source,
        "wheel_sha256": wheel_sha256,
        "module_sha256": _sha256_file(module_path),
        "extension_file": str(extension_path),
        "extension_sha256": _sha256_file(extension_path),
    }


def _partition_queries(facts: list[dict], queries: list[dict]) -> tuple[list, list]:
    """Split queries by whether their answer is UNIQUE in this corpus.

    Only unique-answer queries may be scored by record identity. The rest are
    routed to role-share scoring, because "did the one record I happened to
    designate come back" is not a measurement when nineteen records answer the
    question equally well.
    """
    subject_counts: dict[str, set[str]] = {}
    for f in facts:
        m = _REPORTS_TO.match(f["text"].strip())
        if m:
            subject_counts.setdefault(m.group(1), set()).add(m.group(2))

    unique, ambiguous = [], []
    for q in queries:
        tgt = (q.get("target_text") or "").strip()
        m = _REPORTS_TO.match(tgt) if tgt else None
        n = len(subject_counts.get(m.group(1), set())) if m else 1
        (unique if n <= 1 else ambiguous).append({**q, "valid_answers": n})
    return unique, ambiguous


def _seed(facts: list[dict], db_path: str):
    from yantrikdb._yantrikdb_rust import YantrikDB
    db = YantrikDB.with_default(db_path)
    for f in facts:
        while True:
            try:
                db.record_text(f["text"], memory_type="semantic", namespace="g")
                break
            except Exception as e:  # noqa: BLE001 — backpressure is expected at this rate
                if "queue full" in str(e) or "Backpressure" in type(e).__name__:
                    time.sleep(0.02)
                    continue
                raise
    time.sleep(DRAIN_SECONDS)
    del db
    gc.collect()


def _fresh_copy(base: Path, into: Path) -> Path:
    """Sidecars included — a -wal left behind carries the state this discards."""
    for f in base.parent.glob(base.name + "*"):
        shutil.copy2(f, into / f.name)
    return into / base.name


def _texts(db, q, k=5, trace: list[dict] | None = None):
    observed_at = time.time()
    hits = db.recall_text(q, top_k=k, namespace="g")
    texts = [(h.get("text") or "").strip() for h in hits]
    rids = [(h.get("rid") or "").strip() for h in hits]
    if trace is not None:
        payload = json.dumps(
            {"rids": rids, "texts": texts}, ensure_ascii=True, separators=(",", ":"))
        trace.append({
            "query": q,
            "top_k": k,
            "count": len(texts),
            "ordering_rids": rids,
            "ordering_texts": texts,
            "ordering_signature": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "observed_at_unix": observed_at,
        })
    return texts


# --- v1.3.0: one function per metric, so the runner can transpose the loop ---
#
# Each of these measures ONE metric on ONE instance. The runner calls a single
# metric across every instance back-to-back, which keeps that metric's
# cross-instance comparison inside its own tight window instead of smearing it
# across the whole suite. See _measure_transposed for why that matters.

def _m_precision(db, unique, ambiguous, probes, trace=None) -> float:
    hits = 0
    for q in unique:
        got = _texts(db, q["query"], q.get("top_k", 5), trace)
        t = (q.get("target_text") or "").strip()
        if t and any(t in g or g in t for g in got):
            hits += 1
    return hits / len(unique) if unique else 0.0


def _m_role_share(db, unique, ambiguous, probes, trace=None) -> float:
    """Ambiguous role queries: what fraction of returned relation records put
    the queried entity in SUBJECT position? Independent of which valid answer
    was designated, so a mechanism promoting any correct answer scores."""
    shares = []
    for q in ambiguous:
        m = _REPORTS_TO.match((q.get("target_text") or "").strip())
        if not m:
            continue
        subj = m.group(1)
        rel = [t for t in _texts(db, q["query"], trace=trace) if " reports to " in t]
        if rel:
            shares.append(sum(1 for t in rel if t.startswith(f"{subj} reports to "))
                          / len(rel))
    return statistics.mean(shares) if shares else 0.0


def _m_possessive_top1(db, unique, ambiguous, probes, trace=None) -> float:
    vals = []
    for p in probes:
        a = _texts(db, p["query_possessive"], trace=trace)
        b = _texts(db, p["query_plain"], trace=trace)
        vals.append(1.0 if (a and b and a[0] == b[0]) else 0.0)
    return statistics.mean(vals) if vals else 0.0


def _m_possessive_jaccard(db, unique, ambiguous, probes, trace=None) -> float:
    vals = []
    for p in probes:
        a = set(_texts(db, p["query_possessive"], trace=trace))
        b = set(_texts(db, p["query_plain"], trace=trace))
        vals.append(len(a & b) / len(a | b) if (a | b) else 1.0)
    return statistics.mean(vals) if vals else 0.0


def _m_direction_separation(db, unique, ambiguous, probes, trace=None) -> float:
    seps = []
    for p in probes:
        e = p["entity"]
        sp, op = f"{e} reports to ", f" reports to {e}."

        def share(ts, _sp=sp, _op=op):
            rel = [t for t in ts if _sp in t or _op in t]
            return None if not rel else sum(1 for t in rel if t.startswith(_sp)) / len(rel)

        s = share(_texts(db, p["query_subject"], trace=trace))
        o = share(_texts(db, p["query_object"], trace=trace))
        if s is not None and o is not None:
            seps.append(s - o)
    return statistics.mean(seps) if seps else 0.0


# Drift sensitivity is MEASURED per run, not declared here.
#
# The tempting shortcut is to label rank-based metrics drift-immune and
# set-based ones drift-sensitive. That is wrong, and this gate caught itself
# assuming it: a rank statistic is immune only where the ranks it compares are
# SEPARATED. At a tie for rank 1, decay drift flips the pair and the "immune"
# metric moves like any other. Immunity is a property of the metric AND the
# corpus it is run on, so it has to be measured on the corpus in hand.
METRICS = {
    "precision_at_5_unique_answers_only": _m_precision,
    "role_share_ambiguous_queries": _m_role_share,
    "possessive_top1_agreement": _m_possessive_top1,
    "possessive_jaccard_secondary": _m_possessive_jaccard,
    "direction_separation": _m_direction_separation,
}


def _one_pass(db, unique: list[dict], ambiguous: list[dict],
              probes: list[dict]) -> dict:
    """Retained for callers wanting a single instance's full reading."""
    return {name: fn(db, unique, ambiguous, probes, None)
            for name, fn in METRICS.items()}


def _metric_observation(fn, db, unique, ambiguous, probes,
                        seed_mtime_ns: int) -> tuple[float, dict]:
    """One raw metric reading plus a signature of every ordering it consumed."""
    started = time.time_ns()
    trace: list[dict] = []
    value = fn(db, unique, ambiguous, probes, trace)
    finished = time.time_ns()
    stable_trace = [
        {k: item[k] for k in ("query", "top_k", "count", "ordering_signature")}
        for item in trace
    ]
    signature = hashlib.sha256(
        json.dumps(stable_trace, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return value, {
        "value": round(value, 8),
        "ordering_signature": signature,
        "query_count": len(trace),
        "result_count": sum(item["count"] for item in trace),
        "observed_at_unix": started / 1e9,
        "finished_at_unix": finished / 1e9,
        "seed_age_start_s": round(max(0, started - seed_mtime_ns) / 1e9, 6),
        "seed_age_end_s": round(max(0, finished - seed_mtime_ns) / 1e9, 6),
    }


def _measure_transposed(instances, unique, ambiguous, probes,
                        decay_per_second: float = 5e-8,
                        drift_probe_seconds: float = 8,
                        seed_mtime_ns: int = 0) -> dict:
    """Measure each metric across ALL instances back-to-back, not each instance
    across all metrics.

    WHY. Recency decay is recomputed per query, so scores fall with the wall
    clock (~5e-8/s) at slightly different rates per record — near-ties in a
    degenerate band therefore cross on a seconds timescale. A repeat loop that
    runs instance-by-instance spreads one metric's readings across the whole
    suite, so its spread is mostly drift. v1.2.0 did exactly that and reported
    the result as stdev, i.e. as noise. It wasn't noise; it was the clock.

    Transposing fixes it: one metric across N instances is a burst of a few
    hundred milliseconds, and the drift budget inside that window is printed
    next to the number so a reader can check it against the effect they care
    about rather than trusting the harness.
    """
    out = {}
    for name, fn in METRICS.items():
        observations = [
            _metric_observation(fn, db, unique, ambiguous, probes, seed_mtime_ns)
            for db in instances
        ]
        vals = [value for value, _ in observations]
        raw = [observation for _, observation in observations]
        span = raw[-1]["finished_at_unix"] - raw[0]["observed_at_unix"]

        # Measure drift sensitivity instead of asserting it: read the metric on
        # ONE fixed instance, wait, read it again. Any movement is the clock,
        # because the instance and its data are identical.
        before, before_observation = _metric_observation(
            fn, instances[0], unique, ambiguous, probes, seed_mtime_ns)
        time.sleep(drift_probe_seconds)
        after, after_observation = _metric_observation(
            fn, instances[0], unique, ambiguous, probes, seed_mtime_ns)
        moved = abs(after - before)

        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        out[name] = {
            "mean": round(statistics.mean(vals), 4),
            "values": [round(v, 8) for v in vals],
            "stdev": round(sd, 4),
            "spread": round(max(vals) - min(vals), 4),
            "burst_span_s": round(span, 3),
            "drift_budget_in_burst": f"{span * decay_per_second:.2e}",
            "measured_drift_move": round(moved, 4),
            "drift_probe_seconds": drift_probe_seconds,
            "window": {
                "t_start_unix": raw[0]["observed_at_unix"],
                "t_end_unix": raw[-1]["finished_at_unix"],
                "seed_age_s": raw[0]["seed_age_start_s"],
            },
            "raw_observations": raw,
            "drift_probe": {
                "before": round(before, 8),
                "after": round(after, 8),
                "moved": round(moved, 8),
                "seconds": drift_probe_seconds,
                "before_observation": before_observation,
                "after_observation": after_observation,
            },
            "reading": (
                f"MOVED {moved:.4f} on a fixed instance over "
                f"{drift_probe_seconds}s with no other change — spread here may "
                "be the clock, not the engine; compare the raw ordering "
                "signatures before calling a change a result"
                if moved > 0 else
                "held exactly on a fixed instance across the drift probe — "
                "spread here is not decay, so a change larger than it is real"),
        }
    return out


def _degeneracy(db_path: str, term: str = "taylor") -> dict:
    """Lexical degeneracy, with its denominator IN ITS NAME.

    The engine publishes `bm25_near_best_fraction`: the fraction of FTS-matched
    candidates (the ADMITTED set) whose normalized strength is >= 0.9 of the
    query's best, unrounded. This function publishes something different — the
    fraction of DISTINCT bm25 values among ALL sqlite rows matching the term,
    rounded to 6dp before counting distinctness.

    On query "taylor" the two read 0.4667 and 0.028: a ~17x gap, same concept,
    different denominator and different rounding. They sat under the same name
    ("degeneracy ratio") in two gates that quote numbers at each other, and
    were minutes from appearing side by side in one release note.

    Hence the long name. A metric whose denominator is ambiguous will be
    compared against a metric it does not measure.
    """
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT memories_fts.rank FROM memories m "
        "JOIN memories_fts ON memories_fts.rowid = m.rowid "
        "WHERE memories_fts MATCH ? AND m.consolidation_status='active' "
        "ORDER BY rank", (term,)).fetchall()
    if not rows:
        return {"term": term, "matched": 0}
    ranks = [r[0] for r in rows]
    best = min(ranks)
    tied = sum(1 for r in ranks if round(r, 6) == round(best, 6))
    return {"term": term, "matched": len(rows),
            "distinct_bm25": len({round(r, 6) for r in ranks}),
            "bm25_distinct_ratio_over_all_matches_6dp":
                round(len({round(r, 6) for r in ranks}) / len(rows), 4),
            "not_comparable_to": ("engine bm25_near_best_fraction — that counts "
                                  "near-best strength over ADMITTED candidates, "
                                  "unrounded; this counts DISTINCT values over "
                                  "ALL matching rows at 6dp"),
            "matchers_tied_at_best_rank": tied,
            "fraction_ordered_by_cosine_alone": round(tied / len(rows), 4)}


def _determinism(base: Path, runs: int = 6, seed_mtime_ns: int = 0,
                 query: str = STABILITY_QUERY) -> dict:
    """Identical query, identical starting state, fresh copy each time.

    A build that answers differently on the same bytes cannot be measured at
    all, so this runs FIRST and the report says so loudly.
    """
    from yantrikdb._yantrikdb_rust import YantrikDB
    seen, observations, root = [], [], Path(tempfile.mkdtemp())
    for i in range(runs):
        d = root / f"r{i}"
        d.mkdir()
        db = YantrikDB.with_default(str(_fresh_copy(base, d)))
        trace: list[dict] = []
        texts = _texts(db, query, TOP_K, trace)
        seen.append(tuple(texts))
        item = trace[0]
        item["seed_age_s"] = round(
            max(0.0, item["observed_at_unix"] - seed_mtime_ns / 1e9), 6)
        observations.append(item)
        del db
        gc.collect()
    shutil.rmtree(root, ignore_errors=True)
    n = len(set(seen))
    grouped: dict[tuple[tuple[str, ...], tuple[str, ...]], dict] = {}
    for item in observations:
        key = (tuple(item["ordering_rids"]), tuple(item["ordering_texts"]))
        if key not in grouped:
            grouped[key] = {
                "ordering_rids": item["ordering_rids"],
                "ordering_texts": item["ordering_texts"],
                "count": 0,
                "observed_at_unix": [],
            }
        grouped[key]["count"] += 1
        grouped[key]["observed_at_unix"].append(item["observed_at_unix"])
    return {"query": query, "runs": runs, "distinct": n,
            "signatures": list(grouped.values()),
            "deterministic": n == 1, "raw_observations": observations,
            "note": ("identical inputs produced different answers — every "
                     "metric below is unreliable" if n > 1 else
                     "identical inputs produced identical answers")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=None, help="reuse an already-seeded gate db")
    ap.add_argument("--repeats", type=int, default=7,
                    help="isolated repeats; each runs against a fresh db copy")
    ap.add_argument("--stability-runs", "--determinism-runs",
                    dest="stability_runs", type=int, default=6,
                    help="fresh-copy runs for the byte-identical ordering check")
    ap.add_argument("--drift-probe-seconds", type=int, default=8,
                    help="delay between fixed-instance drift observations")
    ap.add_argument("--direction", action="store_true",
                    help="(retained for compatibility; the subset always runs)")
    args = ap.parse_args()
    if args.repeats < 1:
        ap.error("--repeats must be at least 1")
    if args.stability_runs < 2:
        ap.error("--stability-runs must be at least 2")
    if args.drift_probe_seconds < 0:
        ap.error("--drift-probe-seconds cannot be negative")

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    import yantrikdb

    facts, queries, probes = _load()
    unique, ambiguous = _partition_queries(facts, queries)

    if args.db:
        base = Path(args.db)
    else:
        base = Path(tempfile.mkdtemp()) / "gate.db"
        print(f"seeding {len(facts)} records, draining {DRAIN_SECONDS}s…",
              file=sys.stderr)
        _seed(facts, str(base))

    seed = _seed_snapshot(base)
    run_started_at = time.time()

    # Stage and OPEN every instance before measuring anything. Opening is the
    # slow part; doing it up front is what lets each metric be read across all
    # instances inside a burst rather than across the whole suite.
    root = Path(tempfile.mkdtemp())
    instances = []
    for i in range(args.repeats):
        d = root / f"rep{i}"
        d.mkdir()
        instances.append(YantrikDB.with_default(str(_fresh_copy(base, d))))

    metrics = _measure_transposed(
        instances,
        unique,
        ambiguous,
        probes,
        drift_probe_seconds=args.drift_probe_seconds,
        seed_mtime_ns=seed["latest_mtime_ns"],
    )

    del instances
    gc.collect()
    shutil.rmtree(root, ignore_errors=True)

    determinism = _determinism(
        base, args.stability_runs, seed["latest_mtime_ns"])
    seed_after = _seed_snapshot(base)
    if seed_after["sha256"] != seed["sha256"]:
        raise SystemExit(
            "seed hash changed during measurement — the gate did not run on immutable bytes")

    lexical_degeneracy = _degeneracy(str(base))
    run_finished_at = time.time()
    out = {
        "gate": GATE_NAME,
        "gate_version": GATE_VERSION,
        "engine": _import_metadata(yantrikdb),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "executable": str(Path(sys.executable).resolve()),
        },
        "fixtures": FIXTURE_HASHES,
        "seed": seed,
        "config": {
            "metric_repeats": args.repeats,
            "stability_runs": args.stability_runs,
            "drift_probe_seconds": args.drift_probe_seconds,
            "top_k": TOP_K,
            "stability_query": STABILITY_QUERY,
        },
        "observed": {
            "started_unix": run_started_at,
            "finished_unix": run_finished_at,
            "seed_age_s_at_start": max(0.0, run_started_at - seed["mtime_unix"]),
        },
        "corpus": len(facts),
        "stability": determinism,
        "query_partition": {
            "scored_by_record_identity": len(unique),
            "scored_by_role_share": len(ambiguous),
            "why": ("role queries have multiple valid answers in this corpus; "
                    "scoring them by record identity caps precision by "
                    "construction and penalises correct behaviour"),
            "valid_answers_per_ambiguous_query":
                sorted({q["valid_answers"] for q in ambiguous}),
        },
        "instances": args.repeats,
        "metrics": metrics,
        "lexical_degeneracy": lexical_degeneracy,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
