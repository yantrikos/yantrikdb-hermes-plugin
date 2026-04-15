# Security Policy

## Scope

This document covers security properties of the **YantrikDB Hermes plugin** — the Python code in this directory. Security of the underlying `yantrikdb-server` is covered separately at <https://yantrikdb.com/security/>.

## Token handling

- The YantrikDB bearer token is read from `YANTRIKDB_TOKEN` or the `token` key in `$HERMES_HOME/yantrikdb.json`.
- It is attached to every outgoing request as `Authorization: Bearer <token>` and is never logged. The debug log line for each request includes the request id, method, path, status, and duration — **not** headers.
- `save_config(values, hermes_home)` intentionally does not accept the token: the config wizard writes secrets to `.env`, not the JSON file. (This matches the pattern in mem0 and honcho.)
- The `get_config_schema()` entry for `token` declares `"secret": True` so Hermes' setup flow treats it accordingly.
- 401/403 responses raise `YantrikDBAuthError` — no token value is ever interpolated into the exception message.

## Transport

- The plugin speaks plain HTTP by default (local deployment). For production, point `YANTRIKDB_URL` at an HTTPS endpoint — the `requests`-based client uses the system CA bundle by default, so TLS verification is enforced automatically.
- There is no option to disable TLS verification.
- No credentials are cached to disk by the plugin. The `requests.Session` holds only a connection pool.

## Input validation

- Memory bodies exceeding `YANTRIKDB_MAX_TEXT_LEN` (default 25000 chars) are truncated client-side with a visible `…[truncated]` marker. This prevents the agent from inadvertently sending an unbounded blob that could slow the server or trip load shedding.
- 4xx responses from the server are surfaced to the agent as `tool_error` — they do not trip the circuit breaker, because they represent deterministic caller mistakes the agent should correct.

## Resilience

- Bounded retries on transient 5xx / connection blips (configurable, default 3) prevent unbounded retry storms.
- A circuit breaker opens for 120 s after 5 consecutive transient/server/auth failures so a flapping server cannot wedge Hermes' event loop. See [ARCHITECTURE.md](ARCHITECTURE.md) for state transitions.
- Background threads (prefetch, sync_turn, on_memory_write mirror) are all daemon threads with bounded join timeouts; they cannot prevent Hermes from exiting.

## License boundary (AGPL vs MIT)

- `yantrikdb-server` is licensed AGPL-3.0.
- The plugin code in this directory is shipped under Hermes' MIT license.
- The plugin **connects to** the server over HTTP — it does not embed, statically link, or redistribute any YantrikDB code. This is the same model as any MIT client that talks to an AGPL server. It is legally clean; if you fork or redistribute, the plugin is MIT and the server remains AGPL.

## Reporting a vulnerability

If you find a security issue in the plugin:

1. **Do not** open a public issue.
2. Email `security@yantrikdb.com` with a description and, if possible, a proof-of-concept.
3. Expect an acknowledgement within 3 business days and a fix timeline within 10 business days for confirmed issues.

For issues in `yantrikdb-server` itself, follow the disclosure process at <https://yantrikdb.com/security/>.

For issues in Hermes core, follow the Hermes project's disclosure process.
