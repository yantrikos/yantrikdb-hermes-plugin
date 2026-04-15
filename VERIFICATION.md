# Live verification — 2026-04-14

Real end-to-end verification of the plugin against a live Hermes install talking to a live yantrikdb-server cluster. Captured here so the PR body can cite concrete evidence rather than "tests pass in CI".

## Environment

- **Hermes version**: 0.9.0 (`git clone --depth 1 https://github.com/NousResearch/hermes-agent.git`, 2026-04-14)
- **Host**: LXC 129 on Proxmox node1, IP 192.168.4.54, Ubuntu 24.04 LTS, Python 3.12.3, `uv` 0.11.6
- **YantrikDB cluster**: 3-node raft cluster on homelab (yantrikdb-1/140 leader, yantrikdb-2/141 follower, yantrikdb-witness/142), v0.5.x with encryption auto-generated
- **Namespace used**: `hermes-demo` (isolated from Pranab's production memories)
- **LLM backend**: DeepSeek (`deepseek-chat`, `https://api.deepseek.com/v1`) via Hermes' `--base_url`/`--model` flags

## Bug caught by live testing

Running the first Hermes session surfaced a bug that all 95 unit tests had missed:

**`get_tool_schemas()` guarded the schema list with `self._client is None`, returning `[]` before `initialize()` ran.** Hermes calls `get_tool_schemas()` at provider *register* time (in `MemoryManager._register_provider`) to index `tool_name → provider` for routing, which happens strictly before `initialize()`. With the guard, Hermes never indexed our tool names, so every `yantrikdb_*` tool call from the agent resolved as "Unknown tool" and the model fell back to the built-in `memory` tool.

**Fix**: return `list(ALL_TOOL_SCHEMAS)` unconditionally (except for the cron-context skip, which is set inside `initialize()` — fine, since cron contexts don't register the provider for tool use anyway). Runtime readiness is enforced in `handle_tool_call()`, which is where it belongs.

Added test [`test_schemas_available_before_initialize`](tests/test_provider.py) that asserts the eight tool names are present *before* `initialize()` runs, so this regresses if anyone reintroduces a similar guard. This test alone would have caught the bug offline.

## Verification 1 — plugin discovery

```
$ uv run hermes memory status

Memory status
────────────────────────────────────────
  Built-in:  always active
  Provider:  yantrikdb

  Plugin:    installed ✓
  Status:    available ✓

  Installed plugins:
    • byterover  (requires API key)
    • hindsight  (API key / local)
    • holographic  (local)
    • honcho  (API key / local)
    • mem0  (API key / local)
    • openviking  (API key / local)
    • retaindb  (API key / local)
    • supermemory  (requires API key)
    • yantrikdb  (API key / local) ← active
```

Hermes picked up `plugin.yaml`, ran `is_available()` (returned True because `YANTRIKDB_TOKEN` is set), and marked the plugin active.

## Verification 2 — remember × 3 + stats

User query: "Use the yantrikdb_remember tool three separate times to store: (1) text=My name is Pranab Sarkar, domain=people, importance=0.9 (2) text=I prefer Rust for systems code, domain=preference, importance=0.8 (3) text=I am building YantrikDB a cognitive memory database for AI agents, domain=work, importance=0.9. Then call yantrikdb_stats."

```
🔄 API call #1/6
🤖 I'll store those three memories and then check the database stats.
🔧 3 tool calls:
  yantrikdb_remember(text="My name is Pranab Sarkar", domain="people", importance=0.9)
    → {"rid": "019d8eac-f59b-712b-bae7-f7e38326a16e", "stored": true}
  yantrikdb_remember(text="I prefer Rust for systems code", domain="preference", importance=0.8)
    → {"rid": "019d8eac-f999-7c24-b066-f2c710a38111", "stored": true}
  yantrikdb_remember(text="I am building YantrikDB a cognitive memory database for AI agents", domain="work", importance=0.9)
    → {"rid": "019d8eac-fda4-710b-b84f-c6a427dcec4c", "stored": true}

🔄 API call #2/6
🔧 1 tool call:
  yantrikdb_stats()
    → {"active_memories": 1795, "consolidated_memories": 7, "tombstoned_memories": 4,
       "edges": 1, "entities": 2, "operations": 75068, "open_conflicts": 3, "pending_triggers": 98}

🎯 Final: Stored 3 memories with RIDs [...]. Database stats: 1795 active memories, 0 open conflicts, 98 pending triggers.
```

- Three distinct writes, three distinct rids, returning as-expected `{rid, stored}` envelopes.
- `yantrikdb_stats` round-tripped with the full operational snapshot.
- Active memories went from 1790 (pre-test cluster state) to 1795 = 3 writes from this session + 2 from earlier integration test. Counts line up exactly.

## Verification 3 — explainable recall with `why_retrieved`

This is the differentiator. User query: "Call yantrikdb_recall with query='Pranab Sarkar Rust' and top_k=5. Show me the full JSON response verbatim, especially the why_retrieved field for each result."

```
🔄 API call #1/4
🔧 1 tool call:
  yantrikdb_recall(query="Pranab Sarkar Rust", top_k=5)
    → {"count": 5, "results": [
        {"rid": "019d8eac-f59b-712b-bae7-f7e38326a16e",
         "text": "My name is Pranab Sarkar",
         "score": 1.404,
         "importance": 0.9,
         "domain": "people",
         "created_at": 1776215192.987,
         "why_retrieved": ["semantically similar (0.59)", "recent",
                           "important (decay=0.76)", "keyword_match"]},

        {"rid": "019d8eac-f999-7c24-b066-f2c710a38111",
         "text": "I prefer Rust for systems code",
         "score": 1.195,
         "importance": 0.8,
         "domain": "preference",
         "created_at": 1776215194.009,
         "why_retrieved": ["recent", "important (decay=0.68)", "keyword_match"]},

        {"rid": "019d8ead-1f24-74c6-9580-a2b0f095c1bb",
         "text": "Use the yantrikdb_remember tool three separate times ...",
         "score": 1.017,
         "importance": 0.6,
         "domain": "",
         "created_at": 1776215203.620,
         "why_retrieved": ["recent", "important (decay=0.53)",
                           "keyword_match", "fts_sourced"]},

        {"rid": "019d8902-0d94-79cb-a342-a76311a48ca6",
         "text": "benchmark memory number 644 about topic 44 ...",
         "score": 0.405,
         "importance": 0.5,
         "domain": "benchmark",
         "why_retrieved": ["recent"]},

        {"rid": "019d8902-125d-7721-97d1-6738e7294318",
         "text": "benchmark memory number 663 about topic 13 ...",
         "score": 0.394,
         "importance": 0.5,
         "domain": "benchmark",
         "why_retrieved": ["recent"]}
      ]}
```

- `why_retrieved` is a real array of reason codes per result, not a claim in our README.
- Top 3 results rank by the dimensions our tool description promised: semantic similarity × recency × importance, with keyword/FTS signals layered in.
- Lower-ranked `benchmark` memories from an earlier unrelated session are clearly distinguished — they only have `"recent"` as a reason.
- The agent (DeepSeek) passed through the reason lists verbatim, confirming the provider's JSON shape reaches the model intact.

## What this proves for the PR

1. The plugin loads cleanly in an unmodified Hermes 0.9.0 install.
2. The `MemoryProvider` contract is honored — `is_available` / `initialize` / `get_tool_schemas` / `handle_tool_call` all fire in the expected order.
3. The eight tools (`remember`, `recall`, `forget`, `think`, `conflicts`, `resolve_conflict`, `relate`, `stats`) are registered with Hermes and routable from the model.
4. Wire protocol is correct against a real `yantrikdb-server` — all response field names match (`rid`, `why_retrieved`, `consolidation_count`, `active_memories`, …).
5. The "explainable recall" differentiator is real, not marketing — the server returns reason codes and the plugin passes them through unchanged.
6. The 95 unit tests plus the 2 live integration tests (now in `tests/integration/test_live.py`) form a meaningful coverage lattice.

## Known caveats surfaced during this run

- **Token replication is node-local in the tested cluster build.** A token minted against the leader (node 141 at term 39) was rejected by the new leader (node 140 at term 40) after a raft election. Re-minting on the current leader worked. This is a yantrikdb-server issue (control-plane replication), not a plugin issue — the plugin surfaces a clean `YantrikDBAuthError` with the 401, and the circuit breaker doesn't trip. Worth noting in the PR's troubleshooting section as a real failure mode operators may hit.
- **Auxiliary LLM not configured** on the test box, so Hermes' context compression would drop middle turns without a summary. Irrelevant for this short demo, but worth flagging for anyone trying to run multi-hour sessions with yantrikdb as the only external memory.

## Reproducing this

```bash
# From the workspace root, after a yantrikdb-server is running at $YDB_URL:
YANTRIKDB_INTEGRATION_URL=$YDB_URL \
YANTRIKDB_INTEGRATION_TOKEN=$YDB_TOKEN \
python -m pytest tests/integration/ -v
```

For the live Hermes session, see `VERIFICATION.md` prose above — that path requires an LXC with Hermes installed and is not wrapped into a one-liner (yet).
