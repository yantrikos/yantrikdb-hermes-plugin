# Benchmarks

Two instruments: **recall quality** (`run_recall_bench.py`) and **idle CPU**
(`idle_cpu_bench.py`). Both run locally against an embedded YantrikDB.

## Idle-CPU benchmark

```bash
python benchmarks/idle_cpu_bench.py curve      # idle CPU vs history depth
python benchmarks/idle_cpu_bench.py compare    # 1 engine vs N, same history
python benchmarks/idle_cpu_bench.py gate       # committed thresholds; exits 1 on fail
```

Requires `psutil`. Written to diagnose a field report of the backend burning
~55% of a 32-logical-processor machine **while idle** — the engine's
materializer was polling for unapplied operations using an index that existed
only in a schema migration and never in the base schema, so every database
*created* after that migration walked the whole oplog on every tick
([engine #113](https://github.com/yantrikos/yantrikdb/issues/113)).

Shared instrument: the engine team runs this same code, so neither side
maintains a private measuring stick. Two rules are enforced by the harness
rather than left to whoever runs it:

- **Quiesce on state, not on time.** Sampling blocks until
  `oplog WHERE applied = 0` reads zero. A fixed sleep silently measures the
  materializer *draining* and reports it as idle — that mistake produced a
  12.4%-of-machine reading that was pure artifact, and in another run made 6
  engines look 15× cheaper than 1.
- **Stamp the world.** Every run prints engine version, schema version, and
  host CPU count, so a number that has expired is legible as expired. The
  plugin's own "31.9% → 4.5%" figure stayed reproducible while quietly
  becoming a claim about an engine revision nobody would run again.

The gates: **A** — `cpu(10k) ≤ 1.25 × cpu(1k)` (shape, hardware-independent,
the one that catches the defect returning; pre-fix ≈27×, post-fix ≈1.0×).
**B** — under 5% of one core, median of three.

## Recall benchmark

A reproducible measurement of the YantrikDB Hermes plugin's recall quality —
and of the v0.6 self-tuning lift — against a curated, MIT-clean memory-QA set.
No external or licensed data; everything runs locally against an embedded
YantrikDB with the bundled `potion-2M` embedder.

## Run it

```bash
# baseline recall quality
python benchmarks/run_recall_bench.py

# also measure the self-tuning (reinforcement) lift
python benchmarks/run_recall_bench.py --reinforce

# persist machine-readable + markdown output
python benchmarks/run_recall_bench.py --reinforce --json out.json --markdown out.md
```

Requires the native engine wheel (`pip install 'yantrikdb>=0.7.6'`). The run
is deterministic: same corpus, same embedder, fixed ingest order.

## What it measures

| metric | meaning |
|---|---|
| **recall@k** | fraction of queries where a gold memory is in the top-k |
| **answer-containment@k** | fraction where a top-k result text contains the expected answer substring (stricter, content-level) |
| **MRR** | mean reciprocal rank of the first gold hit |
| **self-tuning lift** | MRR / recall@1 delta after reinforcing each query's gold memory (proves the v0.6 feedback loop improves ranking) |

## How the self-tuning lift is measured

With `--reinforce`, a second provider is built with
`YANTRIKDB_SELF_TUNING_RECALL=true`. Pass 1 scores every query and then calls
`recall(reinforce=[gold_rid])` — simulating an agent marking the memory it
actually relied on. Pass 2 re-scores with no further reinforcement. The MRR
delta between passes is the lift attributable to the feedback loop alone.

Surfaced-only frequency is deliberately *not* a positive boost — only
explicit reinforcement moves ranking, so the benchmark cannot inflate itself
by repeatedly surfacing the same memory.

## Dataset

`dataset.json` — 40 memories and 37 queries across preferences, architecture,
people, work, and infrastructure. Each query paraphrases the fact in its gold
memory; distractors share domain and keywords so recall is non-trivial. Extend
it by adding `{text, domain, importance}` corpus entries and
`{q, gold_ids, gold_substring}` queries.

## Regression guard

`tests/test_recall_benchmark.py` runs the same harness with conservative
floors so a ranking regression (or a broken re-rank) fails CI. It skips
automatically when the native engine wheel isn't installed.
