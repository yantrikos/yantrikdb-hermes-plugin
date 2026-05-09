# Changelog

All notable changes to the YantrikDB Hermes memory plugin.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project aims for semantic versioning once merged into Hermes.

## [0.3.0] — 2026-05-09 — Skill substrate + feature flag

### Added

- **Three new tools (opt-in via feature flag)**: `yantrikdb_skill_search`, `yantrikdb_skill_define`, `yantrikdb_skill_outcome`. Bridges Hermes agents to YantrikDB's `skill_substrate` namespace where agent-authored procedural skills live alongside skills written by other consumers (Lane B SDK, server handlers, WisePick). Hermes-authored entries are tagged `metadata.source=hermes` so any consumer can filter Hermes-authored skills in or out cleanly.
- **`YANTRIKDB_SKILLS_ENABLED` feature flag** — defaults **off**. When unset, the three skill schemas are hidden from `get_tool_schemas()` and any direct skill-tool call short-circuits with a clear error pointing at the env var. Pattern: simple-stays-simple, advanced-reachable. Same shape as yantrikdb-server's bundled-embedder default-on engine feature.
- Client-side schema validation reproducing yantrikdb-server's wrapper checks (skill_id regex, body length, applies_to format, skill_type enum). Embedded mode ships full validation since there's no server in front; HTTP mode validates client-side too as defense-in-depth ahead of the server's own check.
- The load-bearing `applies_to` regex (`^[a-z][a-z0-9_]*$` — no hyphens, no dots) is regression-pinned in `tests/test_provider.py::TestSkillValidation::test_applies_to_REJECTS_HYPHEN` per yantrikdb-server's explicit flag. Anyone naturally writing "applies-to"-style hyphenated tags would corrupt the substrate convention; the test prevents that drift.
- 32 new tests: 9 skill dispatch tests, 3 feature-flag tests, 20 validation tests. Total: **128 tests passing** (was 96).

### Architecture

- **Skill substrate**: namespace `skill_substrate` for skill bodies, `outcome_substrate` for append-only outcome events. `metadata.source=hermes` tags all writes by this plugin. Single shared namespace + metadata filtering rather than sub-namespace, per yantrikdb-server's recommendation: sub-namespace would force every downstream consumer to UNION across N+1 namespaces if they wanted all skills, which is the wrong default for the agentic-loop story.
- **Outcomes are append-only**, never auto-rolled-up onto the parent skill. The "did this skill work?" computation is the agent's pedagogy decision, not the substrate's. Matches the WisePick pattern.
- **Embedded-mode TOCTOU on `on_conflict=reject`**: the uniqueness check is best-effort lookup-then-write rather than transactional (single-agent embedded use is non-racy in practice). HTTP mode preserves server-enforced 409. Documented as semantic difference between modes.
- **Engine surface used**: `db.recall_text(query, top_k, namespace=...)` for skill_search (requires yantrikdb >= 0.7.7; pre-0.7.7 falls back to `db.recall(query=..., namespace=...)`). `db.record_text(body, memory_type="procedural", namespace="skill_substrate", metadata={...})` for skill_define. `db.record_text(...)` to `outcome_substrate` for skill_outcome.

### Lifecycle distinction (worth knowing)

The Hermes plugin now lives alongside Hermes' own filesystem skills (`$HERMES_HOME/skills/*.md`) without overlap:

- **Filesystem skills**: human-authored, durable, version-controlled. Canonical for skills a human wrote and committed.
- **YantrikDB skills**: agent-authored, runtime-evolving, semantic-search-queryable. Canonical for patterns the agent distilled from observed success.

Different *kinds* of canonical, not competing authorities. The model resolves by lifecycle, not by competition.

### Configuration

| Env var | Default | Description |
|---|---|---|
| `YANTRIKDB_SKILLS_ENABLED` | `false` | Set `true` / `1` / `yes` to expose the three skill tools. |

When the flag is off, plugin behavior is identical to v0.2.1 (8 tools, same mode-aware backend selection).

## [0.2.1] — 2026-05-09 — Documentation polish for HN-tier scrutiny

Text-only release. No code changes; no behavioural changes. All findings from yantrikdb-core's post-publish review pass on v0.2.0.

### Changed

- README quality claims now cite the upstream evaluation script (`yantrikos/yantrikdb/scratch/eval_potion_2m.py`) so readers can reproduce the R@5 vs MiniLM-L6-v2 numbers. The "~89% / ~92% / ~95% of MiniLM" approximations are now scoped to that specific eval rather than presented as universal.
- Latency table extended with p99 tail numbers for both backends. Added the honest note that even embedded p99 beats HTTP p50 — and that long-running soak validation is in progress upstream, not concluded.
- New "About the embedder quality claims" section explains corpus-size dependence: at 3 records all vectors look similar (top score ~0.58); at 8+ with real diversity the score range opens up (~0.84). Readers running their own evals on toy corpora won't be surprised by the score collapse.
- New "Explainability is a side effect, not a bolt-on" section pulls a verbatim quote from the live DeepSeek Hermes session showing the model parsing `why_retrieved` reason codes naturally and reflecting them in its own reasoning. Frames the explainability surface as the recall response itself rather than a separate feature.

### Internal

- v0.2.0 commit + tag remain valid; v0.2.1 is the recommended pin for documentation-quality reasons but the on-disk plugin behaviour is identical.

## [0.2.0] — 2026-05-09 — Embedded by default

### Added

- **In-process backend** (`yantrikdb/embedded.py`) wrapping `yantrikdb._yantrikdb_rust.YantrikDB` to the same 8-method surface as the HTTP client. Users running a single Hermes instance no longer need a separate `yantrikdb-server`, Docker, token mint, or URL config. `pip install` and go.
- **Backend factory** (`make_backend()`) selects HTTP vs embedded based on `YANTRIKDB_MODE` env (default `embedded`). Provider's tool dispatch is unchanged — same 8 tools, same hooks, same namespace scoping, same circuit breaker policy.
- **New env config**: `YANTRIKDB_MODE` (`embedded` | `http`), `YANTRIKDB_DB_PATH` (defaults to `$HERMES_HOME/yantrikdb-memory.db`), `YANTRIKDB_EMBEDDER` (`""` for the bundled potion-base-2M, or `potion-base-8M` / `potion-base-32M` for tier-2/3 download paths).
- **Hermes-on-LXC verification for embedded mode** captured in `VERIFICATION.md` — real DeepSeek session, 3× `yantrikdb_remember` + `yantrikdb_recall` + `yantrikdb_stats` all sub-millisecond after one-time 80 ms engine warmup.
- 96 tests passing, all transport-agnostic — they exercise the provider contract, not the backend.

### Changed

- **Default backend is now embedded** (`YANTRIKDB_MODE=embedded`). Users pinning v0.1 behavior should set `YANTRIKDB_MODE=http` explicitly.
- `pip_dependencies` adds `yantrikdb>=0.7.6` (required for the bundled embedder via `YantrikDB.with_default()`). v0.7.6 ships only `uuid-utils` + `click` as hard deps; the install is ~10 MB total.
- `is_available()` now mode-aware: embedded mode is available iff `yantrikdb` is importable; HTTP mode requires a token (unchanged).
- `YantrikDBConfig` extended with `mode`, `db_path`, `embedder_name` fields; HTTP-only fields (`url`, `token`, `connect_timeout`, etc.) and embedded-only fields coexist on one dataclass.

### Performance (steady-state, post-warmup)

| Op | v0.1 HTTP (Apr 14, LXC vs LAN cluster) | v0.2 Embedded (today, in-process) |
|---|---|---|
| `record_text` p50 | ~13.8 ms | **0.60 ms** |
| `recall_text` p50 | ~24.0 ms | **2.58 ms** |
| Token mint at install | required | not needed |
| Server / Docker | required | not needed |
| Cold start (one-time) | n/a | 77 ms |

### Notes for HTTP-mode users

The HTTP backend (`YANTRIKDB_MODE=http`) is unchanged in v0.2 and still recommended for:

- HA cluster deployments where multiple Hermes instances share one yantrikdb-server.
- Multi-tenant scenarios needing the cluster's centralized control plane.
- Auditing setups requiring server-side request logs.

## [0.1.0] — 2026-04-14 — Initial

### Added

- `YantrikDBMemoryProvider` implementing Hermes' `MemoryProvider` ABC.
- Eight tool schemas: `yantrikdb_remember`, `yantrikdb_recall`, `yantrikdb_forget`, `yantrikdb_think`, `yantrikdb_conflicts`, `yantrikdb_resolve_conflict`, `yantrikdb_relate`, `yantrikdb_stats`.
- Explainable recall — the `why_retrieved` reason list from the server is surfaced per result.
- Structured `think()` response with consolidation counts, conflict counts, patterns, duration, and server-suggested triggers.
- Three optional hooks: `on_session_end` (auto-consolidation), `on_pre_compress` (preserves high-salience memories through Hermes context compression), `on_memory_write` (mirrors built-in MEMORY.md / USER.md additions).
- Typed error taxonomy: `YantrikDBAuthError`, `YantrikDBClientError`, `YantrikDBTransientError`, `YantrikDBServerError` on a `YantrikDBError` base.
- Circuit breaker: 5 consecutive transient/server/auth failures → 120 s cooldown. 4xx errors do not trip the breaker.
- Bounded HTTP retries on transient 5xx and connection blips (urllib3 Retry with exponential backoff).
- Per-request `req_id` + `latency_ms` at DEBUG for post-hoc log correlation.
- Client-side text truncation at `YANTRIKDB_MAX_TEXT_LEN` (default 25000) with a visible marker.
- Config resolution: env vars first, `$HERMES_HOME/yantrikdb.json` overlay, numeric coercion, empty-value skip.
- Configurable timeouts and retry count via env: `YANTRIKDB_READ_TIMEOUT`, `YANTRIKDB_CONNECT_TIMEOUT`, `YANTRIKDB_RETRY_TOTAL`.
- Namespace scoping: `{base}:{agent_workspace}:{agent_identity}` for per-identity isolation while allowing cross-session consolidation.
- `get_config_schema()` + `save_config()` so `hermes memory setup` can walk the user through token + URL configuration.
- 94 tests covering config loading, request formation, error taxonomy, tool dispatch, hook semantics, circuit breaker behavior, and text truncation. All tests run without network.

### Deliberate non-goals for this release

- No assistant-message extraction on `sync_turn` (hallucination amplification risk).
- No embedded / in-process YantrikDB — the plugin is a thin HTTP client. (Reversed in v0.2.0.)
- No local SQLite fallback — out of scope.
- No batch write queue — background threads already absorb latency; the added complexity is not justified for v1.
- No CLI subcommand (`hermes yantrikdb …`) — still tracked as future work.
