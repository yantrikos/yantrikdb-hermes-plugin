# yantrikdb-hermes-plugin

[![CI](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-96%20passing-brightgreen)](https://github.com/yantrikos/yantrikdb-hermes-plugin/actions)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/yantrikos/yantrikdb-hermes-plugin)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![YantrikDB](https://img.shields.io/badge/yantrikdb-%E2%89%A50.7.4-orange)](https://github.com/yantrikos/yantrikdb-server)
[![Hermes Agent](https://img.shields.io/badge/hermes--agent-plugin-8a2be2)](https://github.com/NousResearch/hermes-agent)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-checked-2a6db2)](https://mypy-lang.org/)

> **YantrikDB as a memory provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent).** Self-maintaining memory — canonicalizes duplicates, surfaces contradictions, explains recall — in a drop-in plugin. As of **v0.2.0** the default backend is **in-process** (`pip install` and go, no separate server).

This repository tracks the plugin as a standalone artifact so users can install it immediately, without waiting on upstream review. [Issue NousResearch/hermes-agent#9975](https://github.com/NousResearch/hermes-agent/issues/9975) asks whether upstream would welcome it; a PR is in flight. Until that lands, install from here.

## Install

Hermes discovers memory plugins under `plugins/memory/<name>/` in its source tree, so installation is a file copy:

```bash
# In your hermes-agent checkout
cd path/to/hermes-agent/plugins/memory
git clone https://github.com/yantrikos/yantrikdb-hermes-plugin tmp
mv tmp/yantrikdb .
rm -rf tmp
```

Or as a one-liner:

```bash
curl -sSL https://github.com/yantrikos/yantrikdb-hermes-plugin/archive/refs/heads/main.tar.gz \
  | tar -xz -C path/to/hermes-agent/plugins/memory --strip-components=1 \
    yantrikdb-hermes-plugin-main/yantrikdb
```

Then configure:

```bash
hermes config set memory.provider yantrikdb
cat >> ~/.hermes/.env <<EOF
YANTRIKDB_URL=http://localhost:7438
YANTRIKDB_TOKEN=ydb_...
EOF
```

Verify:

```bash
hermes memory status
# → Provider: yantrikdb  Plugin: installed ✓  Status: available ✓
```

A running [`yantrikdb-server`](https://github.com/yantrikos/yantrikdb-server) is required. Docker:

```bash
docker run -d -p 7438:7438 -v yantrikdb-data:/var/lib/yantrikdb \
  --name yantrikdb ghcr.io/yantrikos/yantrikdb:latest
docker exec yantrikdb yantrikdb token --data-dir /var/lib/yantrikdb \
  create --db default --label hermes
```

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

## Verification

- **95 unit tests** covering request formation, error taxonomy, provider contract, hook semantics, circuit breaker, text truncation — all mocked, no network required.
- **2 live integration tests** (`tests/integration/test_live.py`) that exercise the full flow against a real `yantrikdb-server`. Skipped by default; run with `YANTRIKDB_INTEGRATION_URL` + `YANTRIKDB_INTEGRATION_TOKEN` set.
- **End-to-end demo** against an unmodified Hermes 0.9.0 install, captured in **[VERIFICATION.md](VERIFICATION.md)** — real transcripts of a DeepSeek agent calling `yantrikdb_remember` × 3, `yantrikdb_stats`, and `yantrikdb_recall` with the `why_retrieved` reason list coming back through the tool response verbatim.

CI runs ruff + mypy + pytest on Python 3.11 / 3.12 / 3.13 on every push.

## Running the tests

```bash
python -m pytest tests/                          # unit tests
YANTRIKDB_INTEGRATION_URL=http://localhost:7438 \
YANTRIKDB_INTEGRATION_TOKEN=ydb_... \
  python -m pytest tests/integration/ -v         # live integration
```

## Status

**v0.1.0** — live-verified, feature-complete for v1 scope, 95 tests passing, pending Hermes upstream review.

See [yantrikdb/CHANGELOG.md](yantrikdb/CHANGELOG.md) for version history and [yantrikdb/ARCHITECTURE.md](yantrikdb/ARCHITECTURE.md) for the control flow, error taxonomy, and threading model.

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
