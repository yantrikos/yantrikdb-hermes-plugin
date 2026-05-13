# yantrikdb-hermes-plugin

[![CI](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-128%20passing-brightgreen)](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/yantrikos/yantrikdb-hermes-plugin)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![YantrikDB](https://img.shields.io/badge/yantrikdb-%E2%89%A50.7.6-orange)](https://github.com/yantrikos/yantrikdb-server)
[![Hermes Agent](https://img.shields.io/badge/hermes--agent-plugin-8a2be2)](https://github.com/NousResearch/hermes-agent)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-checked-2a6db2)](https://mypy-lang.org/)

> **YantrikDB as a memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).** Self-maintaining memory — canonicalizes duplicates, surfaces contradictions, explains recall — in a drop-in plugin. As of **v0.2.0** the default backend is **in-process** (`pip install` and go, no separate server).

This repository **is** the canonical distribution. Per Hermes maintainer guidance, new memory providers aren't being merged upstream — the recommended pattern is standalone plugins that users install via `pip` and copy into their Hermes tree. That keeps the version cadence, CI gating, issue triage, and review cycle on the plugin author's side, so fixes ship the same day they're ready instead of waiting on upstream review bandwidth.

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

Eight tools exposed to the agent by default: `yantrikdb_remember`, `_recall`, `_forget`, `_think`, `_conflicts`, `_resolve_conflict`, `_relate`, `_stats`. Three additional **opt-in** skill tools (v0.3.0+): `_skill_search`, `_skill_define`, `_skill_outcome` — see [Skills](#skills-opt-in-v030) below.

### Compared to other Hermes memory providers

Each row in the table below is backed by [`tests/comparison/findings_scale_lxc/<provider>/`](tests/comparison/findings_scale_lxc/) — the actual `findings_scale.yaml`, `transcript.md`, and `raw/` response capture from running a 1000-fact + 20-query probe against that provider on a real Hermes 0.9.0 install (LXC 129, commit `4610551`). The corpus is deterministic (`fixtures/corpus_1k.json`, seed=20260512: 600 realistic agent-memory facts + 300 noise + 50 planted duplicates + 50 planted contradictions); the probe is provider-agnostic and reproducible. Methodology details in [`tests/comparison/README.md`](tests/comparison/README.md).

| Provider | Hosting | Verified at 1000 scale | Writes (ok/attempted; latency) | Recall latency | Precision@5 | `why_retrieved` field | Maintenance behaviour observed |
|---|---|---|---|---|---|---|---|
| **yantrikdb** (this) | embedded | yes — 256/1000 writes [^queuecap] | 256/1000; p50 0.48 ms / p99 5.13 ms | p50 3.78 ms / p99 32.94 ms | **0.80** (16/20) | yes — `why_retrieved` per result | contradiction API: `yantrikdb_conflicts`; duplicates kept separate (canonicalisation via explicit `think()`) |
| [byterover](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/byterover) | cloud | couldn't verify — requires `brv` CLI auth | — | — | — | — | — |
| [hindsight](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/hindsight) | cloud-default (local-stub mode used) | yes — 1000/1000 writes | 1000/1000; p50 0.27 ms / p99 0.31 ms | p50 0.28 ms / p99 0.30 ms | **0.00** (0/20) [^localstub] | no | — |
| [holographic](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/holographic) | embedded (SQLite + FTS5) | yes — 1000/1000 writes [^hrrcap] | 1000/1000; p50 23.43 ms / p99 68.52 ms | p50 0.06 ms / p99 0.23 ms | **0.00** (0/20) [^keyword] | no | — |
| [honcho](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/honcho) | self-hosted | couldn't verify — requires honcho-server URL or api key | — | — | — | — | — |
| [mem0](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/mem0) | cloud or self-host | couldn't verify — requires `mem0.api_key` | — | — | — | — | — |
| [openviking](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/openviking) | self-hosted | couldn't verify — requires `OPENVIKING_ENDPOINT` | — | — | — | — | — |
| [retaindb](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/retaindb) | cloud | couldn't verify — requires `RETAINDB_API_KEY` | — | — | — | — | — |
| [supermemory](https://github.com/NousResearch/hermes-agent/tree/main/plugins/memory/supermemory) | cloud | couldn't verify — requires `SUPERMEMORY_API_KEY` | — | — | — | — | — |

[^queuecap]: yantrikdb v0.4.2 plugin against yantrikdb engine 0.7.8 on Linux: the engine's ingest queue is bounded at 256 pending ops and didn't drain during the 1000-fact burst (probe hit `RuntimeError('ingest queue full ...; retry after 50ms')` from fact 257 onward, even with 60-attempt × 100 ms backoff). Recall on the 256 stored facts is solid (P@5 = 0.80). Surfaced upstream as a likely queue-drain regression in the manylinux build of 0.7.8 — the Hermes plugin itself doesn't loop the writes.

[^localstub]: `HINDSIGHT_MODE=local_embedded` was set so `is_available()` returns true without an API key, but in this configuration writes return immediately (sub-millisecond) and recall returns no results — the local mode appears to be a no-op stub rather than a real local backend. Full retrieval almost certainly requires the cloud account.

[^hrrcap]: At ~256 stored items, the engine emits `HRR storage near capacity: SNR=2.00 (dim=1024, n_items=...)` warnings on every subsequent write. The capacity warning is part of holographic's normal output; it's not an error and writes continue to succeed, but retrieval quality is expected to degrade past that point.

[^keyword]: Holographic's recall is keyword-based (FTS5 + HRR cleanup); the probe's queries are full sentences (`"What color scheme does the user prefer in VS Code?"`). The 0/20 result is a query-format mismatch, not a retrieval failure — keyword-shaped queries probably hit. The honest takeaway is that holographic and yantrikdb target different query shapes, not that one is "better".

**Where the verified data lives** — every cell in the table maps to a file:

- `findings_scale.yaml` — structured cells (the table is generated from these by [`compare.py`](tests/comparison/compare.py))
- `transcript.md` — human-readable session log with timing
- `raw/recall-Q*.json` — captured raw recall responses for every query
- `fixtures/corpus_1k.json`, `fixtures/queries_1k.json` — the deterministic corpus + queries used

**How to re-run it** — clone the repo, `scp tests/comparison/` to a Hermes-installed machine, `python3 runner_scale.py --all`. The harness will skip-with-honest-reason for any provider whose `is_available()` returns False (e.g. missing API key); for the ones that initialise, it produces a fresh `findings_scale.yaml`. Pull requests welcomed when accounts unlock more rows.

Three optional lifecycle hooks: `on_session_end` auto-consolidates, `on_pre_compress` preserves high-salience memories through context compression, `on_memory_write` mirrors built-in `MEMORY.md` / `USER.md` additions.

## Skills (opt-in, v0.3.0+)

Skills are **procedural memory**: reusable patterns the agent distills from observed success and pulls back next session. They live in YantrikDB's shared `skill_substrate` namespace alongside skills authored by other consumers (Lane B SDK, server handlers, WisePick). Hermes-authored skills are tagged `metadata.source=hermes` so any downstream consumer can filter them in or out cleanly.

**Disabled by default.** Adding the plugin to an existing Hermes install doesn't change the tool schema the model sees. Enable explicitly when you want the agentic skill loop:

```bash
echo "YANTRIKDB_SKILLS_ENABLED=true" >> ~/.hermes/.env
```

When enabled, three new tools join the schema:

| Tool | Purpose |
|---|---|
| `yantrikdb_skill_search` | Semantic search over agent-authored skills, namespace-isolated from regular memory recall. |
| `yantrikdb_skill_define` | Distill a procedural pattern into a reusable skill (`skill_id`, `body`, `skill_type`, `applies_to`). Client-side validation reproduces yantrikdb-server's wrapper checks. |
| `yantrikdb_skill_outcome` | Record success/failure for a skill after it's used. Append-only event log; rollup is the agent's call, not the substrate's. |

The agentic loop closes: agent observes a successful sequence → distills it via `define` → next session pulls it via `search` → records outcome via `outcome` → over time, ranking reflects what actually works.

**Lifecycle distinction worth understanding.** Hermes' own filesystem skills (`$HERMES_HOME/skills/*.md`) are *human-authored, durable, version-controlled*. YantrikDB skills are *agent-authored, runtime-evolving, semantic-search-queryable*. Different kinds of canonical, not competing authorities. The model picks by lifecycle.

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

**v0.4.2** (current) — first-class embedder loaders for the `model2vec` family and the HF `sentence-transformers` ecosystem; embedding dim auto-probed; default install stays slim via optional `[model2vec]` and `[sentence-transformers]` pip extras. 151 tests passing on Python 3.11/3.12/3.13. **Standalone-by-design** per Hermes maintainer guidance — Hermes is not accepting new memory providers upstream; standalone plugins installed via `pip` are the recommended pattern. PR [#9989](https://github.com/NousResearch/hermes-agent/pull/9989) closed 2026-05-13 with that resolution.

### Release cadence

| Version | Date | Highlight |
|---|---|---|
| v0.1.0 | 2026-04-14 | HTTP backend, 8 tools, 96 tests |
| v0.2.0 | 2026-05-09 | Embedded backend default, ~10 MB install, sub-ms recall |
| v0.3.0 | 2026-05-09 | Skill substrate bridge (opt-in) |
| v0.3.1 | 2026-05-09 | PyPI distribution + `yantrikdb-hermes` CLI installer |
| v0.4.1 | 2026-05-12 | Pluggable embedders (custom Python class via `YANTRIKDB_EMBEDDER_CLASS`) |
| v0.4.2 | 2026-05-12 | First-class `model2vec` + `sentence-transformers` loaders, auto-probed dim |

### Durability signals

The maintainer doesn't promise "I won't quit" — promises like that aren't testable. What's testable:

- Every release ships with tests + CI (Python 3.11 / 3.12 / 3.13) + tagged CHANGELOG + a publish gate where 151 tests + ruff + mypy must pass before the wheel uploads to PyPI.
- First user issue on this repo (multilingual embedding support) was filed and shipped to PyPI the same day — 25 minutes from raised to released.
- Underlying yantrikdb engine: ~5.2k/mo PyPI downloads; flagship server repo has 141 GitHub stars; broader yantrikos namespace ~13.5k/mo combined PyPI+npm. Cross-stack ownership (engine + HTTP server + MCP server + this plugin) — 14+ months of parallel maintenance, not a one-week hobby.
- Independent recognition: accepted into the [Cursor Directory](https://cursor.directory/plugins/yantrikdb) (300k+ developer reach) and (sibling project) the Anthropic MCP Directory.
- Substrate design deposited as a peer-citable preprint: [10.5281/zenodo.20128887](https://doi.org/10.5281/zenodo.20128887).

That's what I can give you. The technical merits are above; the maintenance shape is here so you can audit before adopting.

See [yantrikdb/CHANGELOG.md](yantrikdb/CHANGELOG.md) for full release notes and [yantrikdb/ARCHITECTURE.md](yantrikdb/ARCHITECTURE.md) for the control flow, error taxonomy, and threading model (covering both backends).

## License

This plugin is **MIT** (matching Hermes — the code is intended for upstream contribution). The [YantrikDB server](https://github.com/yantrikos/yantrikdb-server) itself is AGPL-3.0; the plugin only talks to it over HTTP and does not embed or redistribute any server code, so the boundary is the same as any MIT client talking to an AGPL service. See [yantrikdb/SECURITY.md](yantrikdb/SECURITY.md#license-boundary-agpl-vs-mit) for the full note.

## Links

- **Plugin docs**: [yantrikdb/README.md](yantrikdb/README.md)
- **Architecture**: [yantrikdb/ARCHITECTURE.md](yantrikdb/ARCHITECTURE.md)
- **Verification transcripts**: [VERIFICATION.md](VERIFICATION.md)
- **Hermes Agent**: <https://github.com/NousResearch/hermes-agent>
- **YantrikDB server**: <https://github.com/yantrikos/yantrikdb-server>
- **YantrikDB docs**: <https://yantrikdb.com>
