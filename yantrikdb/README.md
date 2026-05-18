# YantrikDB Memory Provider

> **Self-maintaining memory for Hermes.** Canonicalizes duplicates, surfaces contradictions, ranks with recency awareness, and explains recall — instead of behaving like an append-only vector index.

> Installing standalone (until upstream review lands)? See **[../README.md](../README.md)** for the one-liner drop-in install.

## Why YantrikDB?

| Plugin                     | Best at                                               |
|----------------------------|--------------------------------------------------------|
| `mem0`                     | Managed cloud fact extraction with reranking          |
| `honcho`                   | Cross-session user modeling with dialectic Q&A        |
| `byterover` / `supermemory`| Standard semantic vector recall                        |
| `hindsight` / `holographic`| Specialized retrieval schemes                          |
| **`yantrikdb`**            | **Memory maintenance: canonicalization, contradiction tracking, recency ranking, explainable recall, knowledge-graph boosting** |

What the other memory plugins have in common: once a fact is stored, it stays as-is. Duplicate stores pile up, superseded facts silently outrank their replacements, and the agent has no window into *why* a memory came back.

YantrikDB maintains its store. `think()` runs a bounded maintenance pass that canonicalizes near-duplicates, flags contradictions, and re-scores for recency. `recall()` returns a `why_retrieved` reason list per result. `conflicts()` surfaces superseded claims explicitly so the agent resolves them via `resolve_conflict()` rather than hoping last-write-wins.

## Requirements

- A running `yantrikdb-server` (Docker, systemd, or bare binary). The plugin is a thin HTTP client; it does not start or manage the backend.
- A bearer token minted against that server.

## Setup

```bash
hermes memory setup   # select "yantrikdb"
```

Or manually:

```bash
hermes config set memory.provider yantrikdb
cat >> ~/.hermes/.env <<'EOF'
YANTRIKDB_URL=http://localhost:7438
YANTRIKDB_TOKEN=ydb_...
EOF
```

### Running the server

Docker, single node:

```bash
docker run -d -p 7438:7438 \
  -v yantrikdb-data:/var/lib/yantrikdb \
  --name yantrikdb \
  ghcr.io/yantrikos/yantrikdb:latest
```

Mint a token:

```bash
docker exec yantrikdb yantrikdb token \
  --data-dir /var/lib/yantrikdb \
  create --db default --label hermes
# → ydb_abc123...
```

See <https://yantrikdb.com/server/quickstart/> for systemd, HA cluster, and advanced deployments.

## Config

Optional config file: `$HERMES_HOME/yantrikdb.json`. Env vars are the primary source.

| Key               | Env                         | Default                 | Description                                                                        |
|-------------------|-----------------------------|-------------------------|------------------------------------------------------------------------------------|
| `url`             | `YANTRIKDB_URL`             | `http://localhost:7438` | HTTP endpoint.                                                                     |
| `token`           | `YANTRIKDB_TOKEN`           | *required*              | Bearer token from `yantrikdb token create`.                                        |
| `namespace`       | `YANTRIKDB_NAMESPACE`       | `hermes`                | Tenant prefix. Combined with `agent_workspace:agent_identity` at init time.        |
| `top_k`           | `YANTRIKDB_TOP_K`           | `10`                    | Default recall result count (capped at 50 via tool param).                         |
| `owner_scoping`   | `YANTRIKDB_OWNER_SCOPING`   | `false`                 | Optional Hermes gateway scoping: append resolved-owner shard to the namespace.     |
| `include_base_namespace_recall` | `YANTRIKDB_INCLUDE_BASE_NAMESPACE_RECALL` | `true` | With owner scoping, also recall from the base namespace as shared/global legacy memory. |
| `include_legacy_actor_namespace_recall` | `YANTRIKDB_INCLUDE_LEGACY_ACTOR_NAMESPACE_RECALL` | `true` | With owner aliases, also recall old per-actor owner namespaces created before actors were merged. |
| `identity_map_path` | `YANTRIKDB_IDENTITY_MAP_PATH` | empty                 | Optional JSON file mapping platform actors to canonical owners.                    |
| `connect_timeout` | `YANTRIKDB_CONNECT_TIMEOUT` | `5.0`                   | TCP connect timeout (seconds).                                                     |
| `read_timeout`    | `YANTRIKDB_READ_TIMEOUT`    | `15.0`                  | Per-request read timeout.                                                          |
| `retry_total`     | `YANTRIKDB_RETRY_TOTAL`     | `3`                     | Bounded retries on transient 5xx / connection blips (exponential backoff).         |
| `max_text_len`    | `YANTRIKDB_MAX_TEXT_LEN`    | `25000`                 | Hard cap on memory body length; longer text is truncated client-side with a marker.|

## Optional owner scoping for multi-user Hermes gateways

By default, the effective namespace remains `namespace:agent_workspace:agent_identity`, preserving existing behavior. If one Hermes gateway serves multiple users and you want hard memory isolation without changing YantrikDB core, enable owner scoping:

```json
{
  "owner_scoping": true,
  "identity_map_path": "/path/to/identity-map.json"
}
```

Identity map formats:

```json
{
  "owners": {
    "owner:primary-user": {
      "actors": ["whatsapp:actor-a", "telegram:actor-b"]
    }
  }
}
```

or:

```json
{
  "actors": {
    "whatsapp:actor-a": "owner:primary-user",
    "telegram:actor-b": "owner:primary-user"
  }
}
```

Shared groups can be declared in the same identity map:

```json
{
  "actors": {
    "whatsapp:actor-a": "owner:primary-user",
    "telegram:actor-b": "owner:primary-user",
    "whatsapp:actor-c": "owner:secondary-user"
  },
  "groups": {
    "group:household": {
      "members": ["owner:primary-user", "owner:secondary-user"],
      "conversations": ["whatsapp:family-chat", "telegram:family-chat"]
    }
  }
}
```

When enabled, the plugin resolves the current Hermes `platform` + `user_id` to an owner, appends a stable, collision-resistant owner shard to the namespace, and writes `owner_id`, `actor_id`, `actor_owner_id`, `channel`, and `conversation_id` into metadata. The shard preserves the first 32 chars of the original identifier as a debuggable slug plus a sha256-12 suffix; if you want pure-hash sharding without identifier leakage, pre-hash the owner ids in your identity map before passing them in. If no identity map is configured, the actor becomes its own owner by default, so actors are still stored and isolated without any owner config.

If the current `conversation_id` matches a configured group conversation, writes go to that group owner namespace (for example `group:household`) rather than the actor's personal namespace. Personal recalls include the actor's own namespace plus any configured group namespaces where the resolved owner is a current `member`. Removing someone from a group is therefore a config edit: remove their owner id from `groups.<group>.members`, restart/new-session the provider, and future personal recall stops including that group namespace. Historical memories remain under the group owner; nothing is rewritten.

Recall also includes fallback namespaces by default: old per-actor owner namespaces for every actor mapped to the same owner (`include_legacy_actor_namespace_recall=true`), then the base pre-owner namespace (`include_base_namespace_recall=true`). This means memories written as `whatsapp:actor-a` remain visible after `whatsapp:actor-a` and `telegram:actor-b` are mapped to `owner:primary-user`, and older unscoped memories behave as shared/global legacy memory. New writes still go only to the canonical current owner namespace. Set either fallback false if you want stricter recall. This is a plugin/application concern; YantrikDB core does not need to know platform alias policy.

**Operational notes:**

- The identity map is loaded once at `initialize()` and cached for the lifetime of the provider instance. Edits to `identity-map.json` take effect on the next Hermes session, not mid-session — restart the agent (or trigger a fresh session) after updating aliases.
- With N actors mapped to one owner, every recall fires up to N+2 backend calls in HTTP mode (1 owner-scoped + N legacy actor namespaces + 1 base). Sub-millisecond per call in embedded mode is negligible; in HTTP mode set `include_legacy_actor_namespace_recall=false` or `include_base_namespace_recall=false` if latency budget matters more than backward-compatible recall.

## Tools

| Tool                         | Purpose                                                                                  |
|------------------------------|------------------------------------------------------------------------------------------|
| `yantrikdb_remember`         | Store a memory with importance and optional domain.                                      |
| `yantrikdb_recall`           | Explainable recall — each result includes a `why_retrieved` reason list.                 |
| `yantrikdb_forget`           | Tombstone a memory by rid.                                                               |
| `yantrikdb_think`            | Run the bounded maintenance pass. **The differentiator.**                                |
| `yantrikdb_conflicts`        | List open contradictions detected by `think()`.                                          |
| `yantrikdb_resolve_conflict` | Close a contradiction via `keep_winner` / `merge` / `keep_both` / `dismiss`.             |
| `yantrikdb_relate`           | Record a knowledge-graph edge (e.g. *Alice works_at Acme*).                              |
| `yantrikdb_stats`            | Operational snapshot: memory counts, open conflicts, pending triggers.                   |

### Explainable recall

`yantrikdb_recall` returns each result with a reason list so the agent can audit ranking:

```json
{
  "rid": "019d…",
  "text": "User lives in Seattle since 2024",
  "score": 1.1576,
  "importance": 0.9,
  "why_retrieved": [
    "semantic_match",
    "important (decay=0.91)",
    "graph-connected via User"
  ]
}
```

This lets the agent decide whether to trust a match (semantic + graph + importance) versus a weak match (keyword only), and lets you debug why stale memories are outranking fresh ones.

### The `think()` maintenance pass

Call this at natural break points (end of a phase, long user pause, before a `/compact`). One call:

1. **Canonicalizes** near-duplicates — 20 variations of *"user prefers dark mode"* are linked to 1–2 canonical memories. Originals are kept (not deleted) and recall favors canonical forms.
2. **Flags contradictions** — *"CEO is Alice"* stored earlier and *"CEO is Bob"* stored later surface via `yantrikdb_conflicts`. Nothing is silently overwritten.
3. **Optionally mines patterns** — temporal clusters, entity co-occurrences. Off by default (expensive).

The response is structured:

```json
{
  "consolidated": 3,
  "conflicts_found": 1,
  "patterns_new": 0,
  "patterns_updated": 0,
  "duration_ms": 42,
  "triggers": [
    {"trigger_type": "review_conflicts", "urgency": "medium", "source_rids": ["019d…"]}
  ]
}
```

### Conflict-aware memory

`yantrikdb_conflicts` surfaces what `think()` detected:

```json
{"count": 1, "conflicts": [
  {"conflict_id": "cf_1",
   "memory_a": {"rid": "r1", "text": "CEO is Alice"},
   "memory_b": {"rid": "r2", "text": "CEO is Bob"},
   "detection_reason": "entity_collision: CEO",
   "priority": "high"}
]}
```

The agent resolves explicitly:

```python
yantrikdb_resolve_conflict(
    conflict_id="cf_1",
    strategy="keep_winner",
    winner_rid="r2",
    resolution_note="Bob succeeded Alice in March 2026",
)
```

Strategies:

- `keep_winner` — preserve `winner_rid`, tombstone the other.
- `merge` — emit a consolidated `new_text`, tombstone both.
- `keep_both` — both remain (context-dependent truth).
- `dismiss` — close without action (false positive).

## Hooks

| Hook              | Effect                                                                                          |
|-------------------|-------------------------------------------------------------------------------------------------|
| `on_session_end`  | Calls `/v1/think` to consolidate the session before exit.                                       |
| `on_pre_compress` | Seeds recall with the about-to-be-compressed tail so the Hermes compressor preserves insights. |
| `on_memory_write` | Mirrors built-in `MEMORY.md` / `USER.md` additions into YantrikDB.                              |

Hooks are best-effort: they never block the main Hermes loop and failures are logged at DEBUG without propagating.

Assistant-message extraction is intentionally out of v1 scope — storing LLM output as fact is a hallucination-amplification risk. Only user messages are persisted via `sync_turn`.

## Use cases

- **Long-running research agents** where noise accumulates — `think()` periodically canonicalizes.
- **Debate / planning agents** that encounter shifting facts — contradictions get surfaced, not papered over.
- **User-modeling agents** where preferences evolve — recency ranking plus explicit `resolve_conflict` keeps the picture current.
- **Multi-session projects** where the same entities recur — the knowledge graph boosts cross-session recall.

## Resilience

- Bounded HTTP retries (default 3, configurable) with exponential backoff on transient 5xx and connection errors.
- Circuit breaker: after 5 consecutive transient/server/auth failures the plugin short-circuits for 120 s so a flapping server cannot hammer Hermes' event loop. 4xx errors (deterministic caller mistakes) do not trip the breaker.
- All background writes (sync_turn, prefetch, on_memory_write mirror) run on daemon threads with bounded join timeouts; shutdown flushes pending work.
- Every HTTP call is tagged with a short `req_id` and logged at DEBUG with operation and latency for post-hoc correlation in the Hermes log stream.

## Troubleshooting

**`Connection refused`** — server isn't running. Verify with `curl http://localhost:7438/v1/health`.

**`401 / invalid token`** — mint a fresh token against the *running* server, not a previously-stopped instance. See the [quickstart](https://yantrikdb.com/server/quickstart/).

**Recall returns nothing across sessions** — the effective namespace depends on `agent_workspace` + `agent_identity`. Changing profile names changes the namespace. Check startup logs for `YantrikDB connected: ... (namespace: ...)`.

**Circuit breaker open** — the plugin stopped trying after repeated failures. Check the server; the breaker auto-resets after 120 s.

**Text unexpectedly truncated** — memory bodies over `YANTRIKDB_MAX_TEXT_LEN` (default 25000 chars) are clipped client-side with a visible `…[truncated]` marker. Raise the env var if your use case needs longer bodies, but note that the server may enforce its own limits.

## Architecture notes

The plugin is ~700 lines split into `__init__.py` (provider + 8 tool schemas + 3 optional hooks) and `client.py` (HTTP wrapper, typed errors, config). The only pip dependency is `requests`. The Rust backend is an external dependency managed by the user — same model as the `honcho` plugin.

See [ARCHITECTURE.md](ARCHITECTURE.md) for control flow, error taxonomy, and threading model. [CHANGELOG.md](CHANGELOG.md) tracks versioned changes; [SECURITY.md](SECURITY.md) documents the token/secret handling model.

### License

YantrikDB-server is AGPL-3.0. This plugin ships under Hermes' MIT license. The plugin connects to the server over HTTP and does not statically link or embed any YantrikDB code, so the model is the same as any MIT client that talks to an AGPL server — legally clean.

## Links

- Server repo: <https://github.com/yantrikos/yantrikdb-server>
- Docs: <https://yantrikdb.com>
- HTTP API reference: <https://yantrikdb.com/server/http-api/>
- Issues: <https://github.com/yantrikos/yantrikdb-server/issues>
