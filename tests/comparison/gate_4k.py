"""The competing-distractor gate — the one both sides run against a candidate wheel.

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

Real memory is less uniform than this (a 4,353-record production database
scores 3/4 on the same probes), so treat a regression here as a signal about
mechanism, not a forecast of field precision. Weight a production-clone gate
above this one for any user-facing claim.

Corpus and queries are hash-pinned so iterations across candidate wheels stay
comparable. If a hash check fails, the gate refuses to run rather than quietly
reporting numbers from a different corpus.

Run:
    python tests/comparison/gate_4k.py                 # precision + diagnostics
    python tests/comparison/gate_4k.py --pool-sweep    # + cross-encoder sweep
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus_4353_gate_v1.json"
QUERIES = FIXTURES / "queries_1k.json"

# Pinned at gate v1.0.0. A candidate wheel that changes retrieval must be
# measured against the same bytes the previous candidate was measured against.
CORPUS_SHA256 = "2d2d039094644ce5b2a1d8de5047daa5b4a183e98919bdae3a57a67962df4fc9"
QUERIES_SHA256 = "a4b866a5bdeccfb103b25d2cf1a66a1ca50af9096ccb55bf73579f676f4b407f"

# Seeding is async; measuring before the write queue drains reports compaction
# noise as retrieval quality. Learned the hard way — see benchmarks/.
DRAIN_SECONDS = 30


def _load() -> tuple[list[dict], list[dict]]:
    cblob = CORPUS.read_text(encoding="utf-8")
    qblob = QUERIES.read_text(encoding="utf-8")
    for name, blob, want in (("corpus", cblob, CORPUS_SHA256),
                             ("queries", qblob, QUERIES_SHA256)):
        got = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        if got != want:
            raise SystemExit(
                f"{name} hash mismatch — this is not gate v1.0.0.\n"
                f"  expected {want}\n  actual   {got}\n"
                "Numbers from a drifted corpus are not comparable to prior runs."
            )
    return json.loads(cblob)["facts"], json.loads(qblob)["queries"]


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
    return db


def _measure(fn, queries: list[dict], label: str) -> dict:
    hits, lat = 0, []
    for q in queries:
        t0 = time.perf_counter()
        got = fn(q["query"], q.get("top_k", 5))
        lat.append((time.perf_counter() - t0) * 1000)
        tgt = (q.get("target_text") or "").strip()
        if tgt and any(tgt in g or g in tgt for g in got):
            hits += 1
    return {"config": label,
            "precision_at_5": round(hits / len(queries), 3),
            "p50_ms": round(statistics.median(lat), 1)}


def _degeneracy(db_path: str, term: str = "taylor") -> dict:
    """The measurement that distinguishes a set-level promoter from a ranker.

    If distinct_bm25 / matched is near zero, the lexical lane has no remaining
    signal to discriminate WITHIN the matched set, and any boost monotone in
    lex leaves cosine to decide the order by itself.
    """
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT memories_fts.rank, length(m.text) FROM memories m "
        "JOIN memories_fts ON memories_fts.rowid = m.rowid "
        "WHERE memories_fts MATCH ? AND m.consolidation_status='active' "
        "ORDER BY rank", (term,)).fetchall()
    if not rows:
        return {"term": term, "matched": 0}
    ranks = [r[0] for r in rows]
    distinct = len({round(r, 6) for r in ranks})
    best = min(ranks)  # fts5 rank is negative; best == most negative
    tied_at_best = sum(1 for r in ranks if round(r, 6) == round(best, 6))
    return {"term": term, "matched": len(rows), "distinct_bm25": distinct,
            "degeneracy_ratio": round(distinct / len(rows), 4),
            "bm25_spread": round(max(ranks) - min(ranks), 6),
            # Everything tied at the best rank receives lex == 1.0 and is
            # therefore ordered by cosine alone, whatever the boost does.
            "matchers_tied_at_best_rank": tied_at_best,
            "fraction_ordered_by_cosine_alone": round(tied_at_best / len(rows), 4)}


def _claims_coverage(db_path: str, queries: list[dict], db) -> dict:
    """Does the substrate already hold the answer that retrieval is missing?

    Relation direction is extracted at write time into `claims` with src/dst
    intact — precisely the signal cosine destroys. This counts the queries the
    substrate can answer but retrieval does not surface.
    """
    con = sqlite3.connect(db_path)
    txt2rid = {}
    for rid, text in con.execute("SELECT rid, text FROM memories"):
        txt2rid.setdefault((text or "").strip(), rid)
    known_missed, answerable, gp_seen = [], 0, False
    for q in queries:
        tgt = (q.get("target_text") or "").strip()
        if not tgt:
            continue
        rid = txt2rid.get(tgt)
        claim = con.execute(
            "SELECT src, dst, rel_type FROM claims "
            "WHERE source_memory_rid=? AND tombstoned=0", (rid,)).fetchone() if rid else None
        hits = db.recall(q["query"], top_k=5, namespace="g")
        got = any(tgt in (h.get("text") or "") or (h.get("text") or "") in tgt for h in hits)
        if any(h.get("scores", {}).get("graph_proximity", 0.0) for h in hits):
            gp_seen = True
        if claim:
            answerable += 1
            if not got:
                known_missed.append({"query": q["query"], "claim": list(claim)})
    return {
        "claims_total": con.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
        "cognitive_edges_total": con.execute(
            "SELECT COUNT(*) FROM cognitive_edges").fetchone()[0],
        "answer_present_in_claims_with_direction": f"{answerable}/{len(queries)}",
        "substrate_knows_but_retrieval_misses": len(known_missed),
        "any_nonzero_graph_proximity": gp_seen,
        "misses": known_missed,
    }


def _direction_and_possessive(db, top_k: int = 5) -> dict:
    """Gate v1.1.0 subset — reported SEPARATELY from overall precision.

    Two things overall precision cannot see:

    DIRECTION. Each probe uses one entity in both roles. Ground truth is role
    POSITION, not record identity, because this corpus gives every person many
    managers on purpose. A retriever that has lost direction returns the same
    records for "who does X report to" and "who reports to X", scoring ~0
    separation; one that preserves it separates toward 1.0.

    POSSESSIVE. Two queries one apostrophe apart, expressing the same need.
    Jaccard of their result sets should be 1.0. Anything lower is the
    `tokenize()` apostrophe exemption surfacing as retrieval instability.
    """
    probes = json.loads((FIXTURES / "direction_probes_v1_1.json")
                        .read_text(encoding="utf-8"))["probes"]
    seps, jaccards, rows = [], [], []

    def _texts(q):
        return [(h.get("text") or "").strip()
                for h in db.recall_text(q, top_k=top_k, namespace="g")]

    for p in probes:
        e = p["entity"]
        subj_pat = f"{e} reports to "          # e is SUBJECT
        obj_pat = f" reports to {e}."          # e is OBJECT

        def share(texts):
            rel = [t for t in texts if subj_pat in t or obj_pat in t]
            if not rel:
                return None
            return sum(1 for t in rel if t.startswith(subj_pat)) / len(rel)

        s_q, o_q = share(_texts(p["query_subject"])), share(_texts(p["query_object"]))
        sep = None if s_q is None or o_q is None else s_q - o_q
        if sep is not None:
            seps.append(sep)

        a, b = set(_texts(p["query_possessive"])), set(_texts(p["query_plain"]))
        jac = len(a & b) / len(a | b) if (a | b) else 1.0
        jaccards.append(jac)
        rows.append({"entity": e, "subject_role_share": s_q,
                     "object_query_subject_share": o_q,
                     "separation": None if sep is None else round(sep, 3),
                     "possessive_jaccard": round(jac, 3)})

    return {
        "direction_separation": round(sum(seps) / len(seps), 3) if seps else None,
        "direction_separation_note": "0.0 = direction fully lost, 1.0 = perfect",
        "possessive_jaccard_mean": round(sum(jaccards) / len(jaccards), 3),
        "possessive_jaccard_note": "1.0 = an apostrophe changes nothing, as it should",
        "probes": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool-sweep", action="store_true",
                    help="also sweep cross-encoder pool_k (needs sentence-transformers)")
    ap.add_argument("--direction", action="store_true",
                    help="also run the v1.1.0 direction + possessive subset")
    ap.add_argument("--db", default=None, help="reuse an already-seeded gate db")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))
    from _bootstrap import _pin_engine_import
    _pin_engine_import()
    from yantrikdb._yantrikdb_rust import YantrikDB

    import yantrikdb

    facts, queries = _load()
    if args.db:
        db_path, db = args.db, YantrikDB.with_default(args.db)
    else:
        db_path = os.path.join(tempfile.mkdtemp(), "gate.db")
        print(f"seeding {len(facts)} records, then draining {DRAIN_SECONDS}s…",
              file=sys.stderr)
        db = _seed(facts, db_path)

    out = {
        "gate": "hermes-plugin/competing-distractors v1.0.0",
        "engine": yantrikdb.__version__,
        "corpus": len(facts), "queries": len(queries),
        "results": [_measure(
            lambda q, k: [h.get("text") or "" for h in
                          db.recall_text(q, top_k=k, namespace="g")],
            queries, "retrieval only")],
        "lexical_degeneracy": _degeneracy(db_path),
        "claims_lane": _claims_coverage(db_path, queries, db),
    }
    if args.direction:
        out["direction_and_possessive"] = _direction_and_possessive(db)
    if args.pool_sweep:
        for pool in (30, 50, 100, 200):
            try:
                out["results"].append(_measure(
                    lambda q, k, _p=pool: [
                        h.get("text") or "" for h in yantrikdb.recall_reranked(
                            db, q, top_k=k, pool_k=_p, namespace="g")],
                    queries, f"+ cross-encoder, pool_k={pool}"))
            except Exception as e:  # noqa: BLE001
                out["results"].append({"config": f"+ cross-encoder, pool_k={pool}",
                                       "error": f"{type(e).__name__}: {str(e)[:120]}"})
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
