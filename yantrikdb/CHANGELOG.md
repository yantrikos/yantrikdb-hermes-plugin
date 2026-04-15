# Changelog

All notable changes to the YantrikDB Hermes memory plugin.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project aims for semantic versioning once merged into Hermes.

## [0.1.0] — Initial

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
- No embedded / in-process YantrikDB — the plugin is a thin HTTP client. Embedded use is covered by `pip install yantrikdb`.
- No local SQLite fallback — out of scope; would duplicate the embedded variant.
- No batch write queue — background threads already absorb latency; the added complexity is not justified for v1.
- No CLI subcommand (`hermes yantrikdb …`) — tracked as a potential v0.2 addition.
