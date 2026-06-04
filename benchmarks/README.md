# Recall benchmark

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
