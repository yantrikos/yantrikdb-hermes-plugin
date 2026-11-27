"""Cross-engine concurrent revision: what happens, in absolute numbers.

Reproducer for the finding in docs/supersedes-concurrency.md. Two engine
handles on ONE database file (`share_engine=False`), two threads that both
read the same current version and then both try to supersede it.

This is NOT a CI assertion. The code does not guarantee a single winner across
engine instances; the deterministic guarantee is the in-engine case, which
tests/test_provider.py::TestRealConcurrentRevision pins.

    python benchmarks/supersedes_race.py --runs 40
    python benchmarks/supersedes_race.py --runs 40 --mode two-engines
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
import threading
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

emb = importlib.import_module("yantrikdb.embedded")

BODY_V1 = "VERSION ONE original body — a sufficiently long procedure description for the race probe"
BODY_A = "VERSION TWO from A — a sufficiently long procedure description for the race probe"
BODY_B = "VERSION TWO from B — a sufficiently long procedure description for the race probe"


def _client(db: str, share: bool):
    return emb.EmbeddedYantrikDBClient(
        emb.YantrikDBConfig(mode="embedded", db_path=db, share_engine=share)
    )


def one_run(share: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="supersede-race-"))
    db = str(tmp / "store.db")
    seed = _client(db, True)
    v1 = seed.skill_define("race.skill", BODY_V1, "procedure", ["git"])

    a, b = _client(db, share), _client(db, share)
    same_engine = a._db is b._db

    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}
    errors: dict[str, str] = {}

    def make(name, client, body):
        real_current = client._skill_current

        def synced_current(skill_id):
            current = real_current(skill_id)
            barrier.wait(timeout=10)   # both read v1 before either writes
            return current

        client._skill_current = synced_current

        def run():
            try:
                outcome[name] = client.skill_define(
                    "race.skill", body, "procedure", ["git"], supersedes_skill_id="race.skill",
                )
            except Exception as exc:  # noqa: BLE001 - the outcome is the measurement
                outcome[name] = exc
                errors[name] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        return threading.Thread(target=run)

    ta, tb = make("a", a, BODY_A), make("b", b, BODY_B)
    ta.start()
    tb.start()
    ta.join(30)
    tb.join(30)

    winners = {k: v for k, v in outcome.items() if isinstance(v, dict)}
    losers = {k: v for k, v in outcome.items() if isinstance(v, Exception)}
    typed = {k: v for k, v in losers.items() if isinstance(v, emb.YantrikDBClientError)}

    edges = seed._db.linked_records(v1["rid"], direction="inbound", link_type="supersedes") or []
    reader = _client(db, True)
    history = reader.skill_search("VERSION", top_k=50, include_history=True)["skills"]
    current = [h["rid"] for h in history if h.get("is_current")]
    fork_reported = sorted(k for k, w in winners.items() if "concurrent_revisions" in w)

    out = {
        "same_engine": same_engine,
        "winners": sorted(winners),
        "typed_refusals": sorted(typed),
        "other_errors": {k: f"{type(v).__name__}: {v}"[:200] for k, v in losers.items() if k not in typed},
        "edges_on_v1": len(edges),
        "history_rows": len(history),
        "current_versions": len(current),
        "fork_reported_by": fork_reported,
        "warnings": {k: w.get("warning") for k, w in winners.items() if w.get("warning")},
        "tracebacks": errors,
        "store": db,
    }
    # The engine cache is process-lifetime by design and never evicts; a run
    # loop that keeps 3 engines alive per iteration measures memory, not the
    # race (an earlier version of this probe was killed by the OOM watchdog).
    emb.reset_engine_cache()
    return out


def classify(r: dict) -> str:
    n_win, n_ref, n_err = len(r["winners"]), len(r["typed_refusals"]), len(r["other_errors"])
    if n_win == 2:
        return "two winners" + (" + fork reported" if r["fork_reported_by"] else " + NO fork report")
    if n_win == 1 and n_ref == 1:
        return "one winner + typed refusal"
    if n_win == 1 and n_err == 1:
        return "one winner + untyped error"
    return f"other (win={n_win} refused={n_ref} err={n_err})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=40)
    ap.add_argument("--mode", choices=("one-engine", "two-engines", "both"), default="both")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    modes = ("one-engine", "two-engines") if args.mode == "both" else (args.mode,)
    for mode in modes:
        share = mode == "one-engine"
        kinds: Counter = Counter()
        states: Counter = Counter()
        examples: dict[str, dict] = {}
        for i in range(args.runs):
            r = one_run(share)
            k = classify(r)
            kinds[k] += 1
            states[(r["edges_on_v1"], r["current_versions"], r["history_rows"])] += 1
            examples.setdefault(k, r)
            if args.verbose:
                print(f"{i + 1:3d} {k:34s} edges_on_v1={r['edges_on_v1']} "
                      f"current={r['current_versions']} rows={r['history_rows']}", flush=True)

        print(f"\n=== {mode}: n={args.runs} ===")
        for k, v in kinds.most_common():
            print(f"  {v:4d}/{args.runs}  {k}")
        print("  post-state (edges_on_v1, current_versions, history_rows):")
        for k, v in states.most_common():
            print(f"    {v:4d}/{args.runs}  {k}")
        if "two winners" in " ".join(kinds) or any("two winners" in k for k in kinds):
            ex = examples[[k for k in examples if k.startswith("two winners")][0]]
            print("\n  example of the two-winner state:")
            print("   " + json.dumps(
                {k: ex[k] for k in ("winners", "edges_on_v1", "current_versions",
                                    "history_rows", "fork_reported_by", "warnings")},
                indent=2, ensure_ascii=False).replace("\n", "\n   "))
            if "a" in ex["tracebacks"]:
                print("\n  loser traceback (a):\n   " + ex["tracebacks"]["a"].replace("\n", "\n   ")[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
