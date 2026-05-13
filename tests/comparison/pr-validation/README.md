# Hermes PR #9989 end-to-end validation

Proof that the contents of [PR #9989](https://github.com/NousResearch/hermes-agent/pull/9989) (`feat/yantrikdb-memory-plugin` at commit `91ad22a2`) work end-to-end against an actual Hermes 0.9.0 install.

## What this answers

> "Have we validated the PR end-to-end, or just the equivalent code via the standalone CLI installer?"

The standalone repo's [VERIFICATION.md](../../../VERIFICATION.md) covers DeepSeek-driven agent sessions against the PyPI-installed plugin. This file covers the orthogonal question: a maintainer reviewing the PR can clone the branch, drop it into a Hermes tree, and verify it works without touching PyPI.

## Environment

- LXC 129 (yantrik-memory-test) on Proxmox, Debian, Python 3.12
- Hermes 0.9.0 (commit `4610551` from the spranab/hermes-agent fork)
- PR branch `feat/yantrikdb-memory-plugin` at HEAD `91ad22a2` (v0.4.2)
- `yantrikdb` engine 0.7.8 installed via `pip install yantrikdb`

## How to reproduce

```bash
# 1. Fresh clone of the PR branch
git clone --branch feat/yantrikdb-memory-plugin \
  https://github.com/spranab/hermes-agent.git /tmp/hermes-pr-test

# 2. Install the engine
pip install yantrikdb>=0.7.6

# 3. Run the validation script
cd /tmp/hermes-pr-test
YANTRIKDB_MODE=embedded YANTRIKDB_DB_PATH=/tmp/yhp-pr.db \
  python3 /path/to/validate.py
```

[`validate.py`](validate.py) drives Hermes' own `plugins.memory.load_memory_provider("yantrikdb")` through the full 8-tool surface end-to-end. [`transcript.txt`](transcript.txt) is the captured run from 2026-05-12.

## What the run proved

| Step | Observation |
|---|---|
| `load_memory_provider("yantrikdb")` | Returns `YantrikDBMemoryProvider`; `is_available()` = True; 166 ms cold load |
| `initialize(session_id)` | Succeeds in 57 ms (engine warmup + SQLite open + bundled potion-2M attach) |
| `get_tool_schemas()` | 8 tools registered with proper descriptions |
| `yantrikdb_remember × 4` | First 89 ms (engine warmup), subsequent 0.58 ms — matches earlier LXC benchmarks |
| `yantrikdb_recall` | 3.37 ms; **`why_retrieved` field present per result**: `['semantically similar (0.83)', 'recent', 'important (decay=0.70)', 'keyword_match']` |
| `yantrikdb_conflicts` | Callable (returned 0 because `think()` wasn't called to detect them in this short flow) |
| `yantrikdb_stats` | Returns operational snapshot with `active_memories: 4`, `operations: 12`, etc. |

The point is not "yantrikdb is fast" — it's that the **PR contents drop in cleanly to a Hermes tree** and the contract surface (provider class, schemas, hooks, recall response shape including `why_retrieved`) is honoured end-to-end without any installer or post-merge fix-up.
