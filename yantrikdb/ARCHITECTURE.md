# YantrikDB Plugin Architecture

This document explains how the plugin is structured, how requests flow through it, and how failures are contained. Target audience: maintainers reviewing the PR and operators debugging a live deployment.

## Files

| File             | Role                                                                              |
|------------------|-----------------------------------------------------------------------------------|
| `__init__.py`    | `YantrikDBMemoryProvider` (implements Hermes' `MemoryProvider` ABC), 8 tool schemas, 3 optional hooks, circuit breaker. |
| `client.py`      | `YantrikDBClient` — HTTP wrapper, typed error taxonomy, config resolution, bounded retries, text truncation. |
| `plugin.yaml`    | Declared name, version, dependencies (`requests>=2.31`), and the three optional hooks the plugin implements. |
| `README.md`      | User-facing documentation.                                                        |
| `CHANGELOG.md`   | Versioned change history.                                                         |
| `SECURITY.md`    | Token / secret handling, AGPL-vs-MIT boundary, reporting.                         |

## Layered responsibilities

```
┌──────────────────────────────────────────────────────────────┐
│                Hermes MemoryProvider contract                │
│            initialize / prefetch / sync_turn / ...           │
└──────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│           YantrikDBMemoryProvider (__init__.py)              │
│  • tool dispatch + schema surface                            │
│  • namespace derivation (base:workspace:identity)            │
│  • background threads (prefetch, sync, mem-write mirror)     │
│  • circuit breaker (5 failures → 120 s cooldown)             │
│  • exception → tool_error() mapping                          │
└──────────────────────────────────────────────────────────────┘
                                │ self._client
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                YantrikDBClient (client.py)                   │
│  • one requests.Session with keep-alive + pooled adapters    │
│  • urllib3 Retry on 5xx / connection blips                   │
│  • per-request req_id + latency_ms at DEBUG                  │
│  • text truncation before POST /v1/remember                  │
│  • error taxonomy: Auth / Client / Transient / Server        │
└──────────────────────────────────────────────────────────────┘
                                │ HTTP
                                ▼
                    yantrikdb-server (port 7438)
```

The plugin owns no persistent state beyond live threads and the HTTP session. Everything durable lives in yantrikdb-server.

## Request flow

1. **Tool call arrives at Hermes** → Hermes invokes `handle_tool_call(name, args)` on the active provider.
2. **Guardrails**: if the cron skip flag is set or the client is `None`, return `tool_error` immediately; if the circuit breaker is open, return a short-circuit error; otherwise dispatch.
3. **Dispatch** maps the tool name to a `_do_*` method that validates args and calls the HTTP client.
4. **HTTP**: the client composes the request, sends with `(connect_timeout, read_timeout)`, retries transient 5xx, logs the `req_id` + latency, and parses the response.
5. **Error mapping**: 401/403 → `YantrikDBAuthError`; 4xx → `YantrikDBClientError`; 429/503 → `YantrikDBTransientError`; 5xx → `YantrikDBServerError`; network/timeout → `YantrikDBTransientError`.
6. **Dispatcher catches** these in the order above: auth and transient/server trip the breaker; client errors do not.
7. **Result** is serialized to JSON and returned to Hermes as the tool output.

## Namespace scoping

The effective namespace at initialize time is `base:agent_workspace:agent_identity`, where `base` is `YANTRIKDB_NAMESPACE` (default `hermes`). This matches HANDOFF §3:

- Cross-session recall works within an identity.
- Two different identities running against the same server do not pollute each other.
- `session_id` goes in memory metadata, not the namespace, so `think()` can consolidate across sessions.

## Threading model

Three long-lived daemon threads may be active:

- **Prefetch thread** — kicked off by `queue_prefetch()`, calls `recall()`, stores the block for the next turn. Joined with a 3 s timeout before `prefetch()` returns.
- **Sync thread** — kicked off by `sync_turn()`, persists the user message in the background.
- **Memory-write mirror thread** — spawned per `on_memory_write(add, …)` call to mirror built-in MEMORY.md additions.

All three are daemon threads. On `shutdown()`:
1. Pending prefetch and sync threads are joined with a 5 s timeout.
2. The `requests.Session` is closed.

Background failures increment the failure counter via `_record_failure()` but never propagate. Client-level 4xx (bad input) are logged at DEBUG without counting against the breaker.

## Circuit breaker

```
                ┌──────────────┐
                │   CLOSED     │  (normal operation)
                │              │
                │ fail_count<5 │
                └──────┬───────┘
                       │  5th transient/server/auth failure
                       ▼
                ┌──────────────┐
                │    OPEN      │  (short-circuit all calls)
                │              │
                │  cooldown    │
                │   = 120 s    │
                └──────┬───────┘
                       │  cooldown elapsed
                       ▼
                ┌──────────────┐
                │  HALF-OPEN   │  (next call tries; reset on success)
                │              │
                └──────────────┘
```

- 4xx client errors are deterministic — they do not count.
- Auth errors count (token may be stale server-side).
- On success, the counter resets to 0 regardless of prior state.

## Text hygiene

`YANTRIKDB_MAX_TEXT_LEN` (default 25000 chars) caps the memory body. The client truncates at a word boundary, appends a visible `…[truncated]` marker, and logs nothing extra — the marker itself is the operator signal. This prevents silent 400s from the server when an agent tries to remember a very large blob.

## Config resolution

Resolution order matches mem0:

1. `YantrikDBConfig.from_env()` reads all env vars with defaults.
2. If `$HERMES_HOME/yantrikdb.json` exists, values from the JSON file overlay individual fields. Empty strings / `None` are ignored. Numeric fields are coerced; bad values keep the env defaults.
3. If `hermes_constants.get_hermes_home` is unavailable (tests), step 2 is skipped.

This means: env sets a floor, the JSON file is a soft override, and neither partial file is ever fatal.

## What the plugin deliberately does not do

- **Manage the yantrikdb-server lifecycle** — installation, tokens, upgrades, clustering. The user owns that. (HANDOFF §3.)
- **Extract facts from assistant messages** — hallucination amplification risk. (HANDOFF §10.1.) Only user turns are persisted.
- **Run `think()` per turn** — too expensive. Only on session end automatically; manual via `yantrikdb_think` otherwise. (HANDOFF §10.2.)
- **Auto-surface conflicts in every prompt** — bloats the system prompt. The agent calls `yantrikdb_conflicts` on demand. (HANDOFF §10.4.)
- **Retry 4xx errors** — deterministic caller mistakes. Surfaced as `tool_error` so the agent can fix and retry.
- **Fall back to a local SQLite store** — duplicative with `pip install yantrikdb` for embedded use. (HANDOFF §12.)

## Testing posture

94 tests cover:

- Config: env parsing, JSON overlay, numeric coercion, corrupt-file fallback, empty-value handling.
- Client: request formation (URL, method, headers, body) for every endpoint, full error taxonomy (401/403/400/404/429/503/500, timeout, connection error), empty-body and non-JSON-body handling, text truncation.
- Provider: 8 tool schemas present, dispatch to the correct client method, top_k cap, missing-param rejection, unknown-tool rejection, namespace derivation (3 corner cases), circuit breaker threshold / short-circuit / reset, `why_retrieved` pass-through, all three optional hooks, config schema and save_config.

All tests run without network. `tests/conftest.py` stubs `agent.memory_provider` and `tools.registry` so the plugin imports cleanly outside Hermes.
