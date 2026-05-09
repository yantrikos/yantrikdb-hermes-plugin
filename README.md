# yantrikdb-hermes-plugin

[![CI](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-96%20passing-brightgreen)](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/yantrikos/yantrikdb-hermes-plugin)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![YantrikDB](https://img.shields.io/badge/yantrikdb-%E2%89%A50.7.6-orange)](https://github.com/yantrikos/yantrikdb-server)
[![Hermes Agent](https://img.shields.io/badge/hermes--agent-plugin-8a2be2)](https://github.com/NousResearch/hermes-agent)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-checked-2a6db2)](https://mypy-lang.org/)

> **YantrikDB as a memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).** Self-maintaining memory — canonicalizes duplicates, surfaces contradictions, explains recall — in a drop-in plugin. As of **v0.2.0** the default backend is **in-process** (`pip install` and go, no separate server).

This repository tracks the plugin as a standalone artifact so users can install it immediately, without waiting on upstream review. [Issue NousResearch/hermes-agent#9975](https://github.com/NousResearch/hermes-agent/issues/9975) asks whether upstream would welcome it; a PR is in flight. Until that lands, install from here.

## Install (default — embedded backend)

The v0.2.0 default backend is **in-process**: `pip install` and go, no separate server.

Step 1 — drop the plugin into your Hermes checkout:

```bash
cd path/to/hermes-agent/plugins/memory
git clone https://github.com/yantrikos/yantrikdb-hermes-plugin tmp
mv tmp/yantrikdb .
rm -rf tmp
```

Step 2 — install the engine:

```bash
pip install yantrikdb        # ~10 MB; pulls only uuid-utils + click
```

Step 3 — activate:

```bash
hermes config set memory.provider yantrikdb
```

Verify:

```bash
hermes memory status
# → Provider: yantrikdb  Plugin: installed ✓  Status: available ✓
```

That's it. No Docker, no token mint, no URL configuration. The bundled `potion-base-2M` static embedder (~8 MB, dim=64, R@5 ≈ 0.90 vs MiniLM's 0.95) loads on first call (~80 ms one-time warmup) and stays in-process.

**Optional: tier up the embedder** (downloads on first use, cached in user data dir):

```bash
echo "YANTRIKDB_EMBEDDER=potion-base-8M" >> ~/.hermes/.env   # 28 MB, dim=256, ~92% MiniLM
# or potion-base-32M for 121 MB, dim=512, ~95% MiniLM
```

## Install (alternative — HTTP backend, for HA cluster setups)

If you run multiple Hermes instances that need to share one memory store, or you want HA via raft:

```bash
docker run -d -p 7438:7438 -v yantrikdb-data:/var/lib/yantrikdb \
  --name yantrikdb ghcr.io/yantrikos/yantrikdb:latest
docker exec yantrikdb yantrikdb token --data-dir /var/lib/yantrikdb \
  create --db default --label hermes
# → ydb_abc123...

cat >> ~/.hermes/.env <<EOF
YANTRIKDB_MODE=http
YANTRIKDB_URL=http://localhost:7438
YANTRIKDB_TOKEN=ydb_abc123...
EOF
```

Same plugin, same 8 tools, same hooks, same provider contract — just talks HTTP to a separately-managed server instead of running the engine in-process.

Full config, tool reference, troubleshooting: **[yantrikdb/README.md](yantrikdb/README.md)**.

## What it does

The differentiator versus other Hermes memory plugins is not the vector store — it's what happens *after* the write:

| Feature | Plain vector memory | YantrikDB |
|---|---|---|
| Duplicate facts | pile up | canonicalized by `think()` |
| Contradictions | silently overwrite | surfaced via `conflicts()`, closed via `resolve_conflict()` |
| Stale facts | outrank fresh ones | recency-aware ranking without deletion |
| Why did a memory rank? | ¯\\_(ツ)_/¯ | every `recall()` result carries a `why_retrieved` reason list |
| Cross-entity recall | semantic-only | graph edges from `relate()` boost related memories |

Eight tools exposed to the agent: `yantrikdb_remember`, `_recall`, `_forget`, `_think`, `_conflicts`, `_resolve_conflict`, `_relate`, `_stats`.

Three optional lifecycle hooks: `on_session_end` auto-consolidates, `on_pre_compress` preserves high-salience memories through context compression, `on_memory_write` mirrors built-in `MEMORY.md` / `USER.md` additions.

### Explainability is a side effect, not a bolt-on

Every `recall()` result already carries the structured ranking-reason list — that's the engine's standard response shape. The model can *read* it without prompt engineering. From the live Hermes session captured in `VERIFICATION.md`, DeepSeek's natural-language summary of the recall:

> *"All 3 memories returned, ranked by relevance × recency × importance. The top result ranked highest (semantic match + keyword + high importance + recency), followed by [...] (keyword match), then [...] (high importance but no direct keyword overlap)."*

DeepSeek wasn't told the reason codes existed; it parsed them from the tool response and reflected them in its explanation. That's the architectural shape we wanted: the explainability surface is the recall response itself, transport-agnostic, model-agnostic, and visible to anyone who looks at the JSON. No separate "explain" tool. No second LLM call. The cost of explainability is zero because it was never separate.

## Verification

- **96 unit tests** covering request formation, error taxonomy, provider contract, hook semantics, circuit breaker, text truncation, mode-aware availability — all mocked, no network required.
- **2 live integration tests** (`tests/integration/test_live.py`) that exercise the full flow against a real `yantrikdb-server`. Skipped by default; run with `YANTRIKDB_INTEGRATION_URL` + `YANTRIKDB_INTEGRATION_TOKEN` set.
- **End-to-end Hermes demos** against an unmodified Hermes 0.9.0 install for both backends, captured in **[VERIFICATION.md](VERIFICATION.md)** — DeepSeek-driven sessions calling all 8 tools, with `why_retrieved` reason codes flowing through the model's reasoning verbatim.

### Performance (steady-state, post-warmup)

| Op | v0.1 HTTP (Apr 14) | v0.2 Embedded (May 9) |
|---|---|---|
| `record_text` p50 | 13.8 ms | **0.60 ms** |
| `recall_text` p50 | 24.0 ms | **2.58 ms** |
| `record_text` p99 | 55.3 ms | 10.66 ms |
| `recall_text` p99 | 67.2 ms | 13.24 ms |
| Cold start | n/a | 77 ms (one-time) |
| Required infrastructure | yantrikdb-server + token | none |
| `pip install` footprint | wheel + requests | wheel + 2 small libs (~10 MB total) |

Even embedded p99 tail latency is faster than HTTP p50 — bad-case embedded beats typical-case HTTP. Long-running soak validation is in progress upstream ([yantrikos/yantrikdb saga task #2](https://github.com/yantrikos/yantrikdb)); these numbers are 100-iteration micro-benchmarks, not 24-hour production traces.

### About the embedder quality claims

Tier 1 (`with_default()`, ~8 MB) uses [`potion-base-2M`](https://huggingface.co/minishlab/potion-base-2M) via [`model2vec-rs`](https://github.com/MinishLab/model2vec-rs) — a pure-Rust static embedding (lookup table + mean-pool + L2-normalize), no transformer forward pass. Tier 2 (`potion-base-8M`, 28 MB) and Tier 3 (`potion-base-32M`, 121 MB) trade larger model files for higher recall and live behind `set_embedder_named()` (downloaded on first use, cached under user data dir).

**Quality numbers cited in this README are R@5 vs `sentence-transformers/all-MiniLM-L6-v2` (dim=384) on the upstream [evaluation corpus](https://github.com/yantrikos/yantrikdb/blob/main/scratch/eval_potion_2m.py).** The "~89% / ~92% / ~95% of MiniLM" approximations are from that specific eval; your mileage will vary on a different corpus or task. Semantic separation is also corpus-size dependent — at 3 records all vectors look similar (top score ~0.58); at 8+ with real diversity the score range opens up (top score ~0.84). If you're evaluating, run against your own data.

CI runs ruff + mypy + pytest on Python 3.11 / 3.12 / 3.13 on every push.

## Running the tests

```bash
python -m pytest tests/                          # unit tests
YANTRIKDB_INTEGRATION_URL=http://localhost:7438 \
YANTRIKDB_INTEGRATION_TOKEN=ydb_... \
  python -m pytest tests/integration/ -v         # live integration
```

## Status

**v0.2.0** — embedded backend live-verified inside Hermes 0.9.0, 96 tests passing, ~10 MB install. v0.1.0 HTTP path remains supported for HA / multi-instance setups via `YANTRIKDB_MODE=http`. Upstream discussion still open at [hermes-agent#9975](https://github.com/NousResearch/hermes-agent/issues/9975) and PR [#9989](https://github.com/NousResearch/hermes-agent/pull/9989); the standalone install path doesn't depend on either.

See [yantrikdb/CHANGELOG.md](yantrikdb/CHANGELOG.md) for the v0.2.0 changes and [yantrikdb/ARCHITECTURE.md](yantrikdb/ARCHITECTURE.md) for the control flow, error taxonomy, and threading model (now covering both backends).

## License

This plugin is **MIT** (matching Hermes — the code is intended for upstream contribution). The [YantrikDB server](https://github.com/yantrikos/yantrikdb-server) itself is AGPL-3.0; the plugin only talks to it over HTTP and does not embed or redistribute any server code, so the boundary is the same as any MIT client talking to an AGPL service. See [yantrikdb/SECURITY.md](yantrikdb/SECURITY.md#license-boundary-agpl-vs-mit) for the full note.

## Links

- **Plugin docs**: [yantrikdb/README.md](yantrikdb/README.md)
- **Architecture**: [yantrikdb/ARCHITECTURE.md](yantrikdb/ARCHITECTURE.md)
- **Verification transcripts**: [VERIFICATION.md](VERIFICATION.md)
- **Hermes Agent**: <https://github.com/NousResearch/hermes-agent>
- **Upstream discussion**: [hermes-agent#9975](https://github.com/NousResearch/hermes-agent/issues/9975)
- **YantrikDB server**: <https://github.com/yantrikos/yantrikdb-server>
- **YantrikDB docs**: <https://yantrikdb.com>
