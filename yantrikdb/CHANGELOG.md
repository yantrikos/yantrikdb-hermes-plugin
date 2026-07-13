# Changelog

All notable changes to the YantrikDB Hermes memory plugin.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semantic versioning. Distributed standalone per Hermes maintainer guidance (PR #9989 closed 2026-05-13).

## [0.8.0] — 2026-07-13 — The self-directing substrate

v0.7 gave the substrate new primitives (knowledge gaps, tasks). v0.8 wires them into a **loop no other Hermes memory provider can do**: the memory notices what it doesn't know, queues the work, hands the agent its own agenda, and closes the loop when the gap is answered. All additive and opt-in — zero behaviour change by default, no new tools, no new dependencies.

See the loop end-to-end: [`assets/demos/self-directing/`](../assets/demos/self-directing/) (runnable via `python demos/self_directing_memory.py`).

### Features

- **Gap→task automation (NEW, opt-in `YANTRIKDB_AUTO_GAP_TASKS`).** On session end, run `knowledge_gaps()` and create a durable task (`Resolve knowledge gap: <query>`) for each recurring gap not already covered by an open task — so the agent's unanswered questions become actionable to-dos. Bounded per session (`gap_task_max`, default 3), deduped by title, fail-soft and graceful-degrading on engines/servers without the APIs.
- **"Your memory's agenda" block (NEW, opt-in `YANTRIKDB_SURFACE_AGENDA`).** Prepends open tasks + top unresolved knowledge gaps to `system_prompt_block`, so every session opens with what the memory still needs.
- **`gap_max_avg_top_score` config (default 0.5).** The gap threshold is embedder-dependent — the bundled dim-64 potion-2M scores unanswered queries ~0.6, so the engine's default of 0.4 is too strict. Exposed and tunable per embedder.
- **Demo + reproducible GIF.** `demos/self_directing_memory.py` (runnable, no API key) plus a pure-Pillow GIF renderer under `assets/demos/self-directing/` (no VHS dependency).

No tool-surface change (still 21). Several new opt-in config flags, all default to zero-behaviour-change. 311 tests pass on both engine 0.8.0 and 0.9.2; ruff + mypy clean.

## [0.7.1] — 2026-07-13 — Require engine 0.9.2 (recall stability)

Patch release: bumps the engine pin `yantrikdb>=0.9.0` → **`>=0.9.2`** to guarantee two upstream correctness fixes for embedded-mode users. No plugin code changes; the full test suite and the recall benchmark are green on 0.9.2 (recall@1 and MRR both improved vs 0.9.0).

- **0.9.2 — NaN-safe recall.** A NaN-valued embedding could panic the process during `recall()` (Rust ≥1.81's sort detecting a broken total order), which killed the call and surfaced as `-32602` in MCP sessions. The engine now guards NaN/zero norms and uses `f64::total_cmp` for every score sort. Embedded-mode plugin recall inherits the fix.
- **0.9.1 — `set_embedder_named` with the worker pool.** Fixes a 0.9.0 regression where the named/multilingual embedder swap always failed ("requires exclusive access to the engine"). Restores the plugin's named-embedder path (`YANTRIKDB_EMBEDDER=potion-base-8M` / `potion-base-32M`).

## [0.7.0] — 2026-06-29 — Build on the engine: gap-closers, conversation + task storage

Engine **0.9.0 "close the memory gaps"** (and 0.8.0) added exactly the primitives the plugin worked around in v0.6, plus two new first-class storage surfaces. v0.7 builds on them. Four waves, all additive and opt-in / graceful-degradation; existing deployments see zero behaviour change. **Requires `yantrikdb>=0.9.0`** (pin bumped on all surfaces) — which also pulls the engine's Backpressure/compactor reliability fix for long embedded write sessions. No new Python dependencies.

A compatibility spike (isolated 0.9.0 venv) confirmed the prior plugin was already fully compatible with 0.9.0 (full suite green, recall benchmark MRR 0.928→0.932) before any of this work; v0.7 is purely the new capabilities.

### Wave H — Engine-backed hygiene scan (PR #42)

- **`yantrikdb_hygiene` scan now uses engine truth.** Via the new `list_records` API it pages the namespace (bounded, truncation-flagged) and computes `stale_candidates` from real engine stats: low `importance` AND (cold `storage_tier` OR `access_count<=1` OR untouched 30d). This supersedes the v0.6 plugin-side sidecar heuristic as the primary staleness signal; `low_usefulness` (surfaced-but-never-reinforced) remains a complementary overlay. Falls back to the v0.6 path when `list_records` is unavailable. New `list_records` client method (HTTP + embedded parity).

### Wave I — Knowledge gaps (PR #42)

- **`yantrikdb_knowledge_gaps` (NEW tool).** Exposes the engine's `knowledge_gaps()` — queries asked often (`>= min_count`) but answered poorly (avg top recall score `<= max_avg_top_score`). The substrate's *known unknowns*: a direct signal of what your memory is missing. No other Hermes memory provider surfaces this. Engine-global scope (documented); degrades to "not available" on older engines/servers.

### Wave J — Conversation storage (PR #42)

- **Verbatim conversation buffer.** New `record_turn` / `recent_turns` / `clear_turns` client methods (HTTP + embedded). `sync_turn` now also records each user + assistant turn into the engine's bounded, verbatim ring buffer (default on, cheap; `YANTRIKDB_CONVERSATION_BUFFER_ENABLED`) — and it **survives Hermes compression**, complementing the semantic store + `on_pre_compress` gist.
- **`yantrikdb_recent_turns` (NEW tool)** reads the verbatim recent exchange (or `clear=true` to wipe it).
- **Opt-in post-compression surfacing** (`YANTRIKDB_SURFACE_CONVERSATION_BUFFER`): a "## Recent conversation (verbatim)" block in `system_prompt_block`, most useful after a compress when only the gist remains.

### Wave K — Task storage (PR #42)

- **`yantrikdb_tasks` (NEW tool)** — a durable, namespace-scoped task/chore store kept in the substrate (status, priority, subtasks via `parent_id`), action-dispatched (`list`/`add`/`update`/`delete`/`get`). Distinct from ephemeral host TODOs and from engine-generated triggers: agent-authored tasks that persist across sessions. New `task_add/list/get/update/delete` client methods (HTTP + embedded parity).

### Tool surface

18 → 21 tools (`yantrikdb_knowledge_gaps`, `yantrikdb_recent_turns`, `yantrikdb_tasks`). Several new opt-in config flags, all default to zero-behaviour-change. Pin `yantrikdb>=0.7.6` → `>=0.9.0`. 302 tests pass (verified on both 0.8.0 and 0.9.0); ruff + mypy clean.

## [0.6.0] — 2026-06-05 — Prove it, then tune it: benchmarked recall + self-tuning + hygiene

v0.5 made the substrate *active*. v0.6 makes it **accountable**. The plugin has always claimed best-in-class recall; v0.6 ships a reproducible number to back the claim, closes the feedback loop so memories that keep proving useful rank higher over time, and surfaces cleanup opportunities so "self-maintaining" becomes visible instead of implicit. Two waves, both pure-plugin (no engine changes), both opt-in by default — existing deployments see zero behaviour change.

### Wave F — Benchmarked recall + self-tuning (PR #35)

- **F1 reproducible recall benchmark (NEW)** — `benchmarks/run_recall_bench.py` spins up a real embedded YantrikDB in a temp dir, ingests a curated, MIT-clean memory-QA corpus (`benchmarks/dataset.json` — 40 memories, 37 queries), runs the real provider recall path, and scores **recall@k**, **answer-containment@k**, and **MRR**. Deterministic; emits JSON + a markdown table. `tests/test_recall_benchmark.py` asserts conservative floors as a CI regression guard (skips when the native engine wheel is absent). First Hermes memory provider to ship a reproducible recall benchmark.
- **F2 self-tuning recall (NEW, opt-in)** — `YANTRIKDB_SELF_TUNING_RECALL=true` enables a plugin-side feedback ledger (`$HERMES_HOME/yantrikdb-recall-feedback.json`). Pass `recall(reinforce=[rid,...])` with the rids that proved useful; a capped boost (`self_tuning_max_boost`, default `0.15`) lifts reinforced memories and re-ranks *before* the top_k cut, so a repeatedly-useful memory climbs into the returned window. Boosted results are tagged `reinforced (+N)` in `why_retrieved`. **Surfaced-only frequency is never a positive boost** — only explicit reinforcement moves ranking, so recall can't entrench whatever already ranks high. The benchmark's `--reinforce` mode measures the MRR lift directly.

### Wave G — Proactive memory hygiene (PR #37)

- **G1 `yantrikdb_hygiene` tool (NEW)** — `action="scan"` (default) composes engine counters + open contradictions + plugin-side low-usefulness candidates (memories that keep surfacing in recall but were never reinforced) into one digest with a human-readable summary and recommended actions. `action="apply"` runs a consolidation pass (`consolidate=true`) and/or permanently forgets specific rids (`forget_rids=[...]`, looped since the engine has no batch delete). Forgetting also purges the rid from the feedback ledger.
- **G2 passive hygiene surfacing (NEW, opt-in)** — `YANTRIKDB_SURFACE_HYGIENE=true` appends a compact "review candidates" block to `system_prompt_block` so the agent sees stale-memory cleanup opportunities without being asked. Cheap: reads only the local ledger, no engine round-trip.

### Tool surface

17 → 18 tools (`yantrikdb_hygiene` added). `yantrikdb_recall` gains an optional `reinforce` array. Four new config flags (`self_tuning_recall`, `self_tuning_max_boost`, `surface_hygiene`, `hygiene_max_surfaced`), all default-off / zero-behaviour-change. No new hooks, no new dependencies, no engine changes.

### Fixes (community contributions, thanks @Moodow)

- **`yantrikdb_relate` crash in embedded mode** (PR #39) — the embedded backend forwarded a `namespace` kwarg to the engine's `relate()`, which doesn't accept it, raising `TypeError` and tripping the circuit breaker on a single call. The kwarg is no longer forwarded (the public method signature is unchanged) until the engine adds namespace-scoped edges.
- **`sentence-transformers` deprecation warning** (PR #38) — the HF embedder loader now prefers `get_embedding_dimension`, falling back to the deprecated `get_sentence_embedding_dimension`, silencing the startup `FutureWarning` on newer `sentence-transformers` while staying backward-compatible.

## [0.5.0] — 2026-05-31 — Active memory: substrate stops waiting

v0.5 is a thesis release. v0.4.x made the substrate richer; v0.5 makes it **active**. The agent doesn't have to remember memory exists to benefit — every turn, the plugin's `system_prompt_block()` injects relevant memories and skills automatically, surfaces unresolved contradictions, captures the gist before compression, time-filters by natural-language ranges, extracts facts from conversation, and (opt-in) shares discoveries across the user's sibling agents.

Five waves shipped, full design in [docs/v0.5-design.md](docs/v0.5-design.md). End-to-end verified against real Hermes Agent v0.15.1 + qwen3.6:27b-64k via ollama on a Portainer-managed Linux host (docs/v0.5-wave-a-e2e-results.md).

### Wave A — Active memory (PR #28)

- **A1 auto-recall polish** — the existing `queue_prefetch → prefetch` path that already auto-injects per-turn recall now respects two new bounds: `auto_recall_min_score` (default `0.4`) filters low-score noise, and `auto_recall_token_budget` (default `600`) truncates oversized blocks. *E2E proven*: qwen3.6 quoted the recalled memory verbatim, attributed to "my notes."
- **A2 skill auto-attach (NEW)** — `queue_prefetch` also runs `skill_search` on the user message. Matching skills surface in `system_prompt_block` under `## Active skill`. The agent never has to call `skill_search` — the right procedure just appears. Single-turn drain so the same skill doesn't echo across turns. Gated on `skills_enabled`. **First Hermes memory provider to surface a skill body into the prompt without an explicit tool call**. *E2E proven*: qwen3.6 reproduced the skill body verbatim, *"Your own notes already say 'always rebase before merge so history stays linear and reviewable.'"*
- **A3 pending-conflict surface (NEW)** — `conflicts()` unresolved entries auto-surface under `## Pending contradictions in your memory`, polled at most once per 60s. Repeats every turn until `resolve_conflict()` lands.

### Wave B — Auto-extraction + recall filter + stats tool (PR #29)

- **B1 cheap-tier extractor** — new `yantrikdb/extractor.py` with seven high-precision regex patterns (preference, possession, identity, location, url, email, plus `is_user_confirmation`). Pure stdlib, zero new deps, <1ms per turn. `sync_turn` now records candidates with `source="extracted"`, `certainty=0.4`, `metadata.extractor` naming the pattern. **HANDOFF §10.1 carve-out**: when the user's message is a bare confirmation ("yes", "right"), the PRIOR assistant turn becomes eligible for extraction too, tagged `confirmed_by_user=True`. Bare LLM output never extracted otherwise.
- **B2 recall filter + stats tool** — `yantrikdb_recall` now hides `source="extracted"` candidates by default; opt in via `include_candidates=true` per-call or `recall_includes_candidates` config. New `yantrikdb_extraction_stats` tool surfaces per-pattern counts so noisy patterns can be tuned.

### Wave C — Bundled UI + observability tool (PR #30)

- **C1 bundled UI** — `yantrikdb-hermes ui [--port 8767] [--open]` starts a localhost web inspector. Pure-stdlib HTTP server, inline HTML/SVG/JS, no new deps. One page, three sections: constellation (memories as glowing nodes, color-coded by domain), recently-learned skills, unresolved contradictions. Read-only. **NOT** a replacement for [wysie's full dashboard](https://github.com/wysie/yantrikdb-hermes-dashboard) — this is the *first-10-minutes-after-install* tool that ships in the wheel. `/api/snapshot` also serves the raw JSON for tooling.
- **C2 `yantrikdb_observability` tool** — one call returns engine counters + recent extraction activity + recent skills + provider health + a human-readable summary line. Each section degrades gracefully on upstream failure.

### Wave D — Smarter `on_pre_compress` + time-aware recall (PR #31)

- **D1 compression gist** — `on_pre_compress` now distills the middle of the about-to-be-compressed conversation (everything except the last 6 turns Hermes preserves verbatim) into a single-line gist and writes it to substrate with `importance=0.75`, `source="compression_summary"`, `pre_compression=true`. Post-compression recall surfaces the gist like any other memory; the tag lets observability tools distinguish summaries from ordinary records.
- **D2 time-aware recall** — `yantrikdb_recall` now accepts `since` / `until` parameters. ISO timestamps (`"2026-05-29"`), relative phrases (`"today"`, `"yesterday"`, `"last week"`), and duration shorthand (`"7d"`, `"24h"`, `"30m"`, `"2w"`) all work. Pure stdlib datetime parsing. Unparseable input treated as "no filter" rather than erroring out.

### Wave E — Cross-agent shared brain (PR #32) — opt-in, default off

- **E1 shared substrate namespace** — when `YANTRIKDB_SHARED_BRAIN_NAMESPACE` is set, explicit `yantrikdb_remember` writes mirror to that namespace tagged `source="agent:<name>"` (auto-derived from `agent_workspace` when blank). Recall unions local + shared so sibling agents inherit each other's discoveries. The user's coding agent learns "Pranab prefers tabs"; their WhatsApp agent automatically knows. Scope intentionally narrow in v1: only explicit `remember` writes mirror; skills, extracted candidates, compression summaries stay agent-local. Mirror failures swallowed silently — never break the primary write. Single-agent users see zero behaviour change.

### Capability table after v0.5

| | yantrikdb-hermes-plugin v0.5 | mem0 | Letta | Mnemosyne |
|---|---|---|---|---|
| Auto-recall injection per turn | ✓ | ✓ | ✓ | ✗ |
| Skill auto-attach per turn | **✓** | ✗ | ✗ | ✗ |
| Pre/post-emit contradiction warning | **✓** | ✗ | ✗ | ✗ |
| Auto-extraction from user turns | ✓ | ✓ | ✓ | partial |
| Effectiveness ledger (per-pattern stats) | **✓** | ✗ | ✗ | ✗ |
| Bundled visualizer | **✓** | hosted only | hosted only | ✗ |
| Compression-aware snapshotting | **✓** | ✗ | partial | ✗ |
| Time-aware recall | **✓** | partial | partial | ✗ |
| Cross-agent shared brain (opt-in) | **✓** | ✗ | ✗ | ✗ |
| Owner-scoping (per-user isolation) | ✓ | partial | ✗ | ✗ |
| Contradiction tracking + conflicts API | ✓ | ✗ | ✗ | ✗ |
| Agent-authored skills with outcome ledger | ✓ | ✗ | ✗ | ✗ |
| Explainable recall (`why_retrieved` + scores) | ✓ | ✗ | ✗ | ✗ |

### Configuration summary (new env vars / config keys)

```
# Wave A
YANTRIKDB_AUTO_RECALL_MIN_SCORE=0.4
YANTRIKDB_AUTO_RECALL_TOKEN_BUDGET=600
YANTRIKDB_AUTO_SKILL_ATTACH=true
YANTRIKDB_AUTO_SKILL_MIN_SCORE=0.55
YANTRIKDB_AUTO_SKILL_MAX_BODIES=2
YANTRIKDB_SURFACE_PENDING_CONFLICTS=true
YANTRIKDB_PENDING_CONFLICTS_POLL_SECONDS=60.0
YANTRIKDB_PENDING_CONFLICTS_MAX_SURFACED=3

# Wave B
YANTRIKDB_EXTRACTION_ENABLED=true
YANTRIKDB_EXTRACTION_TIER=cheap
YANTRIKDB_EXTRACTION_CERTAINTY=0.4
YANTRIKDB_RECALL_INCLUDES_CANDIDATES=false

# Wave E (opt-in)
YANTRIKDB_SHARED_BRAIN_NAMESPACE=
YANTRIKDB_AGENT_NAME=
```

All defaults preserve pre-v0.5 behaviour for users who don't opt in.

### Tests + quality

- 267 tests pass (+33 across v0.5: 11 Wave A · 17 Wave B · 5 Wave C · 6 Wave D · 5 Wave E)
- ruff + mypy clean
- CI matrix: Python 3.11, 3.12, 3.13, 3.14
- Full Hermes-in-Docker e2e on Portainer + qwen3.6:27b-64k verified Wave A (auto-recall + skill auto-attach) and Wave B (extraction landing with correct metadata, recall filter, stats tool)
- Real-engine harness `hermes-test/scripts/harness_wave_a.py` caught one real bug (A2 schema mismatch) the mocked tests missed — pinned by a regression test

### Backward compatibility

- Every new behaviour is either default-on with conservative thresholds (Wave A) or opt-in via env var (Wave E + tier=llm/embedding).
- `yantrikdb_recall` keeps its previous result shape and adds optional new parameters (`since`, `until`, `include_candidates`).
- Existing tools (`remember`, `forget`, `think`, `conflicts`, `relate`, `stats`, trigger consumers, skills) unchanged.
- New tools: `yantrikdb_extraction_stats`, `yantrikdb_observability`.
- New CLI: `yantrikdb-hermes ui`.

## [0.4.17] — 2026-05-29 — Visible auto-skill crystallization + recall score breakdown

Two wow features in one release. Both are about making invisible work visible — closing observability gaps that have existed since the skill surface (v0.3.0) and the recall surface (v0.1.0) shipped.

### 1. Visible auto-skill crystallization

When the agent defines a skill via `yantrikdb_skill_define`, the plugin now persists a small record `(skill_id, skill_type, applies_to, ts, session_id)` to `$HERMES_HOME/yantrikdb-recent-skills.json` and the **next** session's system prompt surfaces them:

```
## Recently learned skills
- `git.commit_clean` (procedure) scope=git,workflow — 3h ago
- `incident.deploy.allowed_kinds_race` (lesson) scope=incident — 1d ago
The agent defined these in prior sessions. If your task matches any,
call `yantrikdb_skill_search` to retrieve the body.
```

#### Why this exists

Pre-v0.4.17, `skill_define` was a write-only operation from the perspective of future sessions. The model could crystallize a hard-won lesson (`"never resolve allowed_kinds before deploy event"`), the session would end, and **no future session would ever know that skill existed** unless it happened to call `skill_search` with the right query. The skill body was correctly stored — but the *fact* that the agent learned something was silent.

The substrate is doing the work; v0.4.17 makes the work visible.

#### Behaviour

- Recorded only on successful store (`stored=true` from engine). `on_conflict=reject` paths do NOT trigger notification — they aren't new learning.
- Persisted as a JSON list under `$HERMES_HOME/yantrikdb-recent-skills.json`, capped at 10 entries, deduped by `skill_id` so re-defining the same skill replaces the prior entry.
- Surfaced only to **prior** sessions (filtered by `session_id != current`) — the session that just defined a skill already knows it exists; surfacing it would just be noise.
- Time-to-live: 7 days. Skills older than that age out of the prompt; they remain in the substrate, just don't keep advertising themselves forever.
- Up to 5 entries surface per prompt to bound prompt budget.
- Logs `INFO` line on each define so the persisted record is debuggable: `YantrikDB skill defined: <id> (<type>) — will surface in next session prompt`.
- Failures during persist/read are swallowed silently with a `DEBUG` log. This is a UX nicety, not load-bearing; never block the dispatch.

#### Configuration

New flag `YANTRIKDB_SURFACE_RECENT_SKILLS` (default `true`). Set to `false` to disable surfacing while still recording (so a future enable can backfill).

### 2. Recall score breakdown

The engine has long returned a per-result `scores` dict with full component breakdown (`similarity`, `decay`, `recency`, `importance`, `graph_proximity`, `valence_multiplier`) AND a `contributions` sub-dict whose values sum to the final `score`. Pre-v0.4.17 the plugin's `_do_recall` silently dropped this field during compaction. v0.4.17 plumbs it through.

```json
{
  "rid": "019e7229-...",
  "text": "Pranab prefers minimal commit messages",
  "score": 1.17,
  "scores": {
    "similarity": 0.78,
    "decay": 0.50,
    "recency": 0.99,
    "importance": 0.50,
    "graph_proximity": 0.0,
    "valence_multiplier": 1.0,
    "contributions": {
      "similarity": 0.39,
      "decay": 0.10,
      "recency": 0.30,
      "importance": 0.39
    }
  },
  "why_retrieved": ["high similarity", "recently created"]
}
```

#### Why this matters

`why_retrieved` is the qualitative explanation; `scores.contributions` is the quantitative breakdown those reasons sum to. Together they make ranking fully transparent — the agent (or a human debugging recall) can see exactly **why** a result ranked where it did. No opaque "trust me, this is relevant" scores. No second LLM call required to "explain why."

No other Hermes memory provider exposes this. Combined with `why_retrieved`, recall results are now the most transparent in the ecosystem.

### Backward compatibility

- `scores` is purely additive on recall results. Existing parsers that key off `rid`/`text`/`score`/`why_retrieved` are unaffected.
- `surface_recent_skills` defaults on; deployments that don't want it set the env var or config key to false.
- Tests: 221 passing (+10 new) — `TestRecallScoreBreakdown` (2) and `TestRecentSkillsCrystallization` (8).

## [0.4.16] — 2026-05-28 — Structured tool envelope (silent-failure-confabulation fix)

Closes a structural agent-protocol gap surfaced by a sibling workspace (yantrikdb-agi) after a real incident: when a tool call failed during a YDB cluster restart, the agent's narrative LLM described success that did not happen. Same pathology as LLM hallucination on absent retrieval — applied to action history rather than knowledge.

### Why this exists

Pre-v0.4.16, tool responses carried the failure signal but not unambiguously:

```json
{"error": "engine unreachable"}
```

When the agent's LLM was later asked "what did you just do?", it reasoned over conversation history. The failure wasn't loudly present in machine-readable form, so the model confabulated plausible completion ("Pranab was updated via telegram_send" — but no telegram ever reached). Same pathology with `skill_define` calls described in narrative but never reaching substrate.

### The envelope

Every tool response now carries the same four envelope fields:

```json
{
  "status": "ok" | "failed",
  "ok": true | false,
  "tool": "yantrikdb_<name>",
  "ts": 1748394801.42,
  ...tool-specific keys preserved verbatim
}
```

Failure responses additionally carry `error` (legacy key, kept) and `reason` (alias). Both equal the same human-readable message; alias surfaces the term LLMs commonly scan for.

### Why two signals (`status` + `ok`)

- `status: "failed"` — primary LLM-readable signal. The word "failed" is loud during narrative summarization; "ok"/"failed" parses more clearly than `false` as a string.
- `ok: false` — primary machine-readable signal. Boolean check for programmatic consumers.
- Belt-and-suspenders, equivalent in current shape, gives flexibility if we later add partial-success semantics.

### What did NOT change (back-compat)

All existing tool-specific response keys preserved verbatim — `rid`, `stored`, `results`, `count`, `acknowledged`, `dismissed`, `acted`, etc. Existing agent code that reads those keys continues working unchanged. The envelope is purely additive.

### What did change

- Module-level `tool_error()` shadows the import from `tools.registry` to add the envelope on every error response
- Dispatcher (`handle_tool_call`) wraps every `_do_*` return via `_wrap_dispatch()` which adds the envelope fields without touching tool-specific payload keys
- Every `tool_error()` call from inside `_do_*` methods is also enveloped — dispatcher backfills the `tool` field when the inner caller omitted it

### Tests

- **211 unit tests pass** (up from 204; 7 new in `TestStructuredEnvelope`):
  - Success envelope on remember (and back-compat keys preserved)
  - Failure envelope on missing required param (direct `tool_error` from `_do_*`)
  - **Failure envelope on backend unavailable** — simulates the exact YDB-cluster-restart scenario yantrikdb-agi flagged
  - Envelope on unknown tool
  - Envelope on cron-context skip (early-return path)
  - Envelope on skills-disabled short-circuit
  - Comprehensive sweep: every dispatch branch (14 tools) carries the envelope

### Credit

Cross-workspace heads-up from yantrikdb-agi, 2026-05-27. Captured to memory at rid `019e6c27` for any future agent built on YantrikDB.

## [0.4.15] — 2026-05-22 — Auto-acknowledge triggers (safe-by-default)

Closes [#22](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/22) from **@alienos**. v0.4.13 shipped the trigger consumer tools, but they're tools — they only do anything if the agent (LLM) calls them. Under the default Hermes CLI flow, an LLM may never bother, so pending triggers accumulated up to the engine's 7-day TTL.

### Added

- **`YANTRIKDB_AUTO_ACKNOWLEDGE_TRIGGERS=true`** (default off). When set, the plugin's session-end hook auto-`acknowledge`s every pending trigger after `think()` runs. Conservative semantics chosen on purpose: `acknowledge` not `act_on` (no action was actually taken) and not `dismiss` (signal isn't discarded as a false positive).
- Loops in 50-trigger batches until the queue is drained, with a safety cap of 10 batches (500 triggers/session) so teardown stays bounded. If the cap fires, a WARNING is logged — sustained high trigger production may be a signal the user should investigate.
- HTTP-mode 404 is now a loud WARNING (not silent debug). yantrikdb-server hasn't shipped the `/v1/triggers/*` endpoints yet; if the user sets the flag in HTTP mode, they're told auto-ack is unavailable rather than left with the false impression it's working. Tracking upstream.

### Trigger lifecycle docs

- Engine triggers have a 7-day TTL (`expires_at = created_at + 604800s`) so accumulation is bounded even without the flag — but that's not a useful ceiling for production.
- The four consumer tools (`pending_triggers`, `acknowledge_trigger`, `dismiss_trigger`, `act_on_trigger`) from v0.4.13 still work the same way; this release just adds an automatic fallback when the agent doesn't drive them.

### Tooling

- Fixed the `[tool.bumpversion]` regex that caused v0.4.14 → v0.4.16 double-bumps. The search pattern `version = "{current_version}"` was matching both `[project] version = "..."` and `[tool.bumpversion] current_version = "..."` (since the latter ends with `version`). Now anchored to start-of-line with `regex = true`.

### Verified

- 204 unit tests pass (10 in `TestOnSessionEnd` cover flag-off default, queue drain, batch looping, HTTP-mode 404 warning, fail-soft per-trigger, think-failure short-circuit, listing-failure swallow, empty-queue handling).
- End-to-end against engine v0.7.17: planted memories produce triggers via `think()`, `on_session_end()` with flag-on drains the queue to 0; flag-off correctly leaves the queue alone.

### Credit

Thanks to **@alienos** for the safe-by-default framing — their 6th substantive contribution.

## [0.4.14] — 2026-05-22 — Manifest version sync

Fixes [#19](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/19) from **@alienos**. v0.4.13 bumped `pyproject.toml` to 0.4.13 but missed `yantrikdb/plugin.yaml`, which Hermes reads to display the plugin version. The v0.4.13 wheel on PyPI shipped with `plugin.yaml: 0.4.12`; `hermes plugins list` would consequently show 0.4.12 even on a fresh `pip install yantrikdb-hermes-plugin==0.4.13`.

v0.4.14 ships with both files synced. Functionally identical to v0.4.13 — no code changes, no API changes. Recommended upgrade path: `pip install -U yantrikdb-hermes-plugin` straight to 0.4.14.

### Credit

Thanks to **@alienos** for opening [PR #19](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/19) within hours of the v0.4.13 release. Their plugin.yaml fix is preserved verbatim as the first commit on this release; the version bump to 0.4.14 sits on top so the corrected manifest reaches PyPI. Fifth substantive contribution from this reporter (#4, #9, #15, #17, #19 — all closed cleanly).

## [0.4.13] — 2026-05-22 — Trigger consumer tools

Closes [#17](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/17) from **@alienos**. v0.4.12 exposed the producer side of the trigger lifecycle (`yantrikdb_think` returns triggers, `yantrikdb_stats.pending_triggers` shows the count) but no consumer tools — so triggers accumulated indefinitely. This release closes that loop.

### Added

- **`yantrikdb_pending_triggers`** — list triggers waiting for agent attention. Accepts `limit` (default 10, capped at 100).
- **`yantrikdb_acknowledge_trigger`** — mark a trigger as seen by the agent, close it. Internally auto-calls the engine's `deliver_trigger` first to satisfy the lifecycle prerequisite.
- **`yantrikdb_dismiss_trigger`** — close a trigger as a non-issue (false positive / out of scope).
- **`yantrikdb_act_on_trigger`** — close a trigger with an action-taken audit-trail entry. Also auto-delivers first.

The tool surface goes from 11 → 15 (or 12 → 12 base when skills are off, since the 4 trigger tools are base-tier).

### Notes on lifecycle semantics (verified against engine v0.7.17)

- A trigger lives at `status=pending` after `think()` produces it.
- `dismiss_trigger` removes it from the pending queue immediately.
- `acknowledge_trigger` and `act_on_trigger` require the trigger to be `delivered_at` first; the plugin transparently calls `deliver_trigger` so the agent doesn't need to know about this step.
- `get_trigger_history` retains audit-trail entries after close — that primitive isn't exposed as a tool in v0.4.13 (it's a substrate-inspection surface, not an agent decision-making one).

### HTTP-mode note

yantrikdb-server doesn't ship `/v1/triggers/*` endpoints yet. In embedded mode (the default for Hermes plugin deployments) the tools work end-to-end via the bundled engine. HTTP-mode callers will receive a 404 from the server until those endpoints land — tracked upstream against yantrikos/yantrikdb-server.

### Credit

Thanks to **@alienos** for the careful diagnosis. The producer/consumer asymmetry was exactly the place to look; fourth substantive issue from this reporter (after #4, #9, #15 closed cleanly).

## [0.4.12] — 2026-05-18 — Quiet HuggingFace embedder

Bugfix landing [#15](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/15) from **@alienos**. The `SentenceTransformerEmbedder` (selected by `YANTRIKDB_EMBEDDER_HF`) was leaking tqdm progress bars to stdout on every memory write. Under Hermes the plugin's stdout is the agent's own output stream, so per-write `Batches: 0%|...` bars polluted agent output and could interfere with log parsing or TTY rendering.

### Fixed

- **`SentenceTransformerEmbedder.encode()`** now passes `show_progress_bar=False` to `sentence_transformers.SentenceTransformer.encode`. Affects the startup probe (where the loader confirms the model works) and every runtime encode call from the engine. Internal fix — no API change, no env-var change, no breaking behaviour for existing callers.
- **README** adds an "Optional: quiet the HuggingFace embedder" section documenting the complementary env vars (`HF_HUB_DISABLE_PROGRESS_BARS=1`, `TRANSFORMERS_VERBOSITY=error`, `HF_HUB_OFFLINE=1`) for the HF Hub auth warning + transformers-library output that the plugin can't suppress from inside.

### Credit

Thanks to **@alienos** for the bug report, root-cause diagnosis, and the working-fix workaround in [#15](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/15). Third confirmed fix from this reporter (after #4 + #9 closed cleanly).

## [0.4.11] — 2026-05-18 — Shared group owner scopes on top of owner scoping

Lands [#14](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/14) from **@wysie** — seventh PR in the arc, building directly on v0.4.10's owner-scoping foundation. Adds shared group namespaces so memories created in a configured group conversation are recallable by every current group member across platforms, while personal-DM memories stay isolated.

Opt-in via the same `YANTRIKDB_OWNER_SCOPING=true`. No new env vars — group config lives in the existing identity-map JSON.

### Added

- **`groups` key in the identity map JSON.** Declare a shared group namespace with `members` (list of canonical owner ids that may recall from it during personal recall) and `conversations` (list of platform-prefixed conversation ids whose writes route to the group namespace):
  ```json
  {
    "actors": {
      "whatsapp:actor-a": "owner:primary-user",
      "telegram:actor-b": "owner:primary-user"
    },
    "groups": {
      "group:household": {
        "members": ["owner:primary-user", "owner:secondary-user"],
        "conversations": ["whatsapp:family-chat", "telegram:family-chat"]
      }
    }
  }
  ```
- **Conversation-to-group routing.** A message written inside a configured group conversation stores under the group namespace instead of the sender's personal-owner namespace. Provenance metadata records `owner_id: group:household`, `actor_owner_id: owner:primary-user`, and `actor_id: whatsapp:actor-a` so writes are still attributable to the human.
- **Group membership in personal recall.** A user's DM recall transparently searches the group namespaces they are listed as members of, in addition to their own owner namespace. Non-members do not get those groups — privacy boundary verified by test.
- **Group-context recall is group-scoped.** When a user is currently in a group conversation, recall is scoped to the group namespace (plus legacy/base fallbacks if enabled). Personal memories don't bleed into group context.
- **2 new tests** covering write-routing (`test_group_conversation_writes_to_configured_group_namespace`) and member-only recall fallback (`test_personal_recall_includes_configured_group_memberships`). 183 tests total (was 181); CI green Python 3.11/3.12/3.13/3.14.

### Notes

- Membership changes are app/config operations and not retroactive: existing memories written under a group namespace stay there. Removing a user from `members` revokes their personal-recall access to that group on the next session.
- Conversation→group mapping is first-match-wins by iteration order; if the same conversation id is listed in multiple groups, only the first matched wins silently. Use distinct conversation ids per group.
- The plugin enforces only the configured allow-list; identity-map updates require restarting the agent session (the map is loaded once at `initialize()`).

### Credit

[@wysie](https://github.com/wysie) — seventh PR. Arc: #6 → #7 → #8 → #10 → #11 → #13 → #14. Second consecutive capability-shaping PR (after #13's owner-scoping foundation), now adding the shared-group layer on top.

## [0.4.10] — 2026-05-17 — Optional owner-scoped namespaces for multi-user Hermes gateways

Lands [#13](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/13) from **@wysie** — sixth PR in the arc, and the first to add new capability rather than fix a regression. One Hermes gateway can now hard-isolate memories across multiple users without requiring YantrikDB core to know anything about platform alias policy.

Opt-in via `YANTRIKDB_OWNER_SCOPING=true`. Default behavior is unchanged.

### Added

- **`owner_scoping` mode.** When enabled, the plugin resolves the current Hermes `platform` + `user_id` to a canonical owner and appends a stable, collision-resistant owner shard to the effective namespace: `{base}:{agent_workspace}:{agent_identity}:owner:{shard}`. New actors automatically get their own isolated shard without any config; mapping is only needed when you decide multiple actors are the same person.
- **Identity map** (`identity_map_path` or `identity_map_json`) supports two natural JSON shapes — flat `{"actors": {"platform:id": "owner:id"}}` or nested `{"owners": {"owner:id": {"actors": [...]}}}`. Both contribute to the merged alias table.
- **Memory metadata provenance.** Every write under `owner_scoping=true` carries `owner_id`, `actor_id`, `channel`, and `conversation_id` so downstream consumers can filter by gateway context.
- **Legacy recall fallback** (default on, configurable). When you introduce an alias map mid-deployment, recall transparently searches: (1) the owner-scoped namespace, (2) each per-actor namespace mapped to the same owner (`include_legacy_actor_namespace_recall=true`), and (3) the base pre-owner namespace (`include_base_namespace_recall=true`). Means memories written before aliasing remain visible after — no rewrite, no migration. New writes still go only to the canonical owner-scoped namespace. Set either fallback false to opt out.
- **New env vars / config keys**: `YANTRIKDB_OWNER_SCOPING`, `YANTRIKDB_INCLUDE_BASE_NAMESPACE_RECALL`, `YANTRIKDB_INCLUDE_LEGACY_ACTOR_NAMESPACE_RECALL`, `YANTRIKDB_IDENTITY_MAP_PATH`, `YANTRIKDB_IDENTITY_MAP_JSON`. All also accepted in `$HERMES_HOME/yantrikdb.json` and reflected in `provider.system_prompt_block()` when active.
- 6 new tests in `tests/test_provider.py` covering owner shard creation, default-no-map fallback, write metadata propagation, full recall fallback chain, multi-actor merge, and disable-base-fallback. 181 tests total (was 175); CI green Python 3.11/3.12/3.13/3.14.

### Notes

- The owner shard preserves the first 32 chars of the original identifier as a debuggable slug plus a sha256-12 suffix. If you want pure-hash sharding without identifier leakage, pre-hash owner ids in your identity map before passing them in.
- The identity map is loaded once at `initialize()`. Edits to `identity-map.json` take effect on the next Hermes session, not mid-session.
- With N actors mapped to one owner, each recall fires up to N+2 backend calls in HTTP mode (1 owner-scoped + N legacy + 1 base). Sub-ms per call in embedded mode is negligible; in HTTP mode disable either fallback flag if latency budget matters.
- This is a plugin/application concern; YantrikDB core continues to operate purely on namespaces + metadata, no platform alias awareness required.

### Credit

[@wysie](https://github.com/wysie) — sixth PR. The arc now reads: #6 symlink installer → #7 venv/uv docs → #8 shim fix → #10 stats-namespace fix → #11 provider session hardening → #13 owner-scoped namespaces. First five fixed regressions or closed gaps; this one shapes plugin direction.

## [0.4.9] — 2026-05-14 — Provider session hardening + embedded signature parity

Lands [#11](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/11) from **@wysie** — fifth PR in this arc, this one a substantive five-concern hardening pass on long-lived provider state. Plus [#12](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/12) from us, closing the embedded-backend signature gap #11's namespace propagation would otherwise have introduced.

### Added (from #11)

- **`YANTRIKDB_SYNC_USER_MESSAGES` and `YANTRIKDB_AUTO_THINK_ON_SESSION_END` env vars are now read by `YantrikDBConfig.from_env()`.** The config fields already existed but weren't wired to env, so users couldn't disable ambient user-message sync or automatic session-end maintenance from outside their config file. Both default `True` (existing behavior preserved).
- **`on_session_switch(new_session_id, *, parent_session_id="", reset=False)` lifecycle hook.** Hermes can change session id inside a long-lived process (resume / branch / pre-compress); the provider now updates its cached `_session_id`, joins any in-flight prefetch/sync threads, and selectively clears prefetch cache entries (everything on `reset=True`; just the prior session on resume/branch).
- **Session-scoped prefetch cache.** `_prefetch_result: str` (single global slot — last-write-wins, sessions could cross-contaminate) became `_prefetch_results: dict[str, str]` keyed by session id. `prefetch()` falls back to a `__default__` slot for callers that don't pass `session_id` yet.
- **Namespace propagation through `think`, `conflicts`, `relate`, and session-end maintenance `think`.** The provider derives a per-identity namespace from base config + Hermes workspace; previously `remember`/`recall`/`stats` honored it but the maintenance and graph endpoints went to the engine's constructor-time namespace. Now consistent across all paths on both HTTP and embedded backends.
- **Embedded-engine error mapping.** `_map_engine_error()` classifies engine `RuntimeError` strings ("queue full", "retry after", "database locked", "busy", "timeout" → `YantrikDBTransientError`; "invalid", "bad rid", "not found" → `YantrikDBClientError`; else → `YantrikDBServerError`) and `remember`/`recall`/`think`/`conflicts`/`resolve_conflict`/`relate`/`stats` are all wrapped. Engine backpressure and locked-database errors now surface as transient (retriable) rather than as raw engine exceptions that would trip the breaker.

### Fixed (from #12)

- **`EmbeddedYantrikDBClient.think()` and `.relate()` accept `namespace` kwarg.** #11's namespace propagation widened the HTTP client signatures but not the embedded ones; in embedded mode (the default `pip install` backend) every `yantrikdb_think` / `yantrikdb_relate` tool call would have `TypeError`'d on the unexpected `namespace=…` kwarg. `tests/test_provider.py` uses a mocked client that accepts any kwargs, so the gap was invisible to the existing suite.
- **`tests/test_signature_parity.py`** (new): inspects both client classes (no instantiation, no engine binary) and asserts every kwarg `YantrikDBClient` accepts on a provider-dispatched method is also accepted by `EmbeddedYantrikDBClient`. Asymmetric on purpose — embedded may have local-only extras, but missing something HTTP exposes is the production-break shape. Catches the next instance of mock-vs-real signature drift at test time rather than at user-report time.

### Credit

[@wysie](https://github.com/wysie) — fifth PR. The arc now reads: #6 symlink installer → #7 venv/uv docs → #8 shim fix for #6's silent breakage → #10 stats-namespace fix → #11 provider session hardening across five concerns. Each PR independently substantive, each with its own tests, each catching something the prior pass missed.

## [0.4.8] — 2026-05-14 — Scope `yantrikdb_stats` to the derived namespace

Lands [#10](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/10) from **@wysie** — fourth PR this stretch, catching another silent inconsistency we hadn't noticed.

### Fixed

- **`yantrikdb_stats` was querying the wrong namespace.** YantrikDB derives a per-identity runtime namespace from the configured base plus Hermes workspace/identity, e.g. `hermes:hermes:default`. `remember` and `recall` already used this derived namespace, but `stats` went through the backend at the *base* config namespace (`hermes`). Result: `hermes memory status` (or any direct `yantrikdb_stats` tool call) could report **zero active memories** while the derived runtime namespace actually contained plenty — a silent UX inconsistency that misled anyone trying to verify their setup.
- Fix: pass the derived namespace through `yantrikdb_stats` so it reports against the same namespace `remember`/`recall` operate on.
- Both embedded and HTTP backends updated to accept an optional `namespace` arg on `stats`.
- New regression test in `tests/test_client.py` pinning the namespaced-stats request shape.

### Credit

[@wysie](https://github.com/wysie) — fourth PR in the same arc that started with #6 (#6 symlink installer → #7 venv/uv docs → #8 shim fix for the symlink-was-actually-broken bug → #10 derived-namespace stats fix). Each one independently substantive, each one with its own test coverage.

## [0.4.7] — 2026-05-14 — Shim installer replaces symlink; `yantrikdb-hermes uninstall`

Lands [#8](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/8) from **@wysie** — third PR this evening, this one catching a real bug we both missed in the v0.4.6 symlink approach.

### Fixed

- **Option B was silently broken on some installs.** v0.4.6's `yantrikdb-hermes install` created `$HERMES_HOME/plugins/yantrikdb/` as a symlink to the pip-installed `yantrikdb_hermes_plugin` package directory. Hermes' user-plugin loader then imported that directory under the synthetic namespace `_hermes_user_memory.yantrikdb`, where the provider's `from .client import …` style relative imports failed silently — and `hermes memory status` would report the plugin as not available. Smoke-tests during v0.4.6 development didn't catch it; wysie's repro did.
- **Fix: shim directory instead of symlink.** `yantrikdb-hermes install` now creates a tiny shim at `$HERMES_HOME/plugins/yantrikdb/` with its own `__init__.py` that does `from yantrikdb_hermes_plugin import YantrikDBMemoryProvider` — an *absolute* import using the pip package's real name, which sidesteps the synthetic-namespace issue entirely. The provider code lives in `site-packages/yantrikdb_hermes_plugin/` and its relative imports resolve normally because it's loaded by its real package name, not under Hermes' synthetic prefix.
- Same fix-class as v0.4.5's top-level `__init__.py` synthetic-parent-module workaround (for the `hermes plugins install` path), reached from the other direction: instead of pre-registering a synthetic parent, route the import through the real package.

### Added (this release)

- **`yantrikdb-hermes uninstall`** subcommand — removes the user-plugin registration at `$HERMES_HOME/plugins/yantrikdb/` (works on shim, copy, or any existing target). Idempotent: prints "not found" and exits 0 when nothing's registered. Includes next-step prompts (choose another provider, optional pip-uninstall, restart Hermes gateway if running).
- **2 new tests** in `tests/test_cli_installer.py`: shim shape (verifies `from yantrikdb_hermes_plugin import …` in the generated `__init__.py`), uninstall removes the registration, uninstall is idempotent when nothing's installed.
- README "Uninstalling" section covering both Option A and Option B clean-removal paths.

### Migration

Existing v0.4.6 users on Option B should re-run `yantrikdb-hermes install --force` after upgrading — that replaces the broken symlink with the working shim. No data loss; memory DB stays at its configured path. v0.4.6 users on Option A or on the legacy `<hermes_root>` positional path are unaffected.

### Credit

[@wysie](https://github.com/wysie) — three PRs in one evening (#6 symlink-default installer, #7 venv/uv docs, this one #8 catching that #6's approach was actually broken and fixing it). Reasoned diagnosis, reproducible test, clean test coverage on the fix. Real first-external-contributor experience.

## [0.4.6] — 2026-05-14 — Symlink-by-default installer (community contribution); Windows fallback

Lands [#6](https://github.com/yantrikos/yantrikdb-hermes-plugin/pull/6) from **@wysie** — first external contribution to this repo. The `yantrikdb-hermes install` CLI now defaults to creating a **symlink** at `$HERMES_HOME/plugins/yantrikdb/` pointing at the pip-installed provider source, so subsequent `pip install --upgrade yantrikdb-hermes-plugin` calls flow through to Hermes automatically without re-running the installer. The previous behaviour (copy files into `<hermes-root>/plugins/memory/yantrikdb/`) is preserved as a backward-compat fallback when a positional `<hermes_root>` argument is given.

### Added (from #6)

- **`yantrikdb-hermes install` (no args)** now installs as a user plugin under `$HERMES_HOME/plugins/yantrikdb/` via a symlink to the pip-installed provider package. Pip upgrades pick up automatically.
- **`--copy` flag** to install a physical copy instead of a symlink (for filesystems / platforms that don't support symlinks).
- **`--hermes-home <path>`** to override the default `$HERMES_HOME` / `~/.hermes` target.
- **`-f` / `--force`** to overwrite an existing target.
- **`yantrikdb-hermes path`** subcommand prints the on-disk path of the installed provider source — useful for users wanting to symlink manually.
- **Legacy `yantrikdb-hermes install <hermes_root>`** (positional argument) still works and copies into `<hermes_root>/plugins/memory/yantrikdb/` for users following the old README flow.
- **`tests/test_cli_installer.py`** — 4-test coverage of the new CLI paths (symlink default, copy mode, refuses existing target without `--force`, legacy positional path).
- **Exit codes**: 0 success / 2 invalid hermes_root / 3 target exists without --force / 4 Windows symlink failure (this release).

### Fixed (this release, on top of #6)

- **Windows symlink fallback**: `Path.symlink_to` requires admin or developer-mode on Windows. Without this fix end-users on stock Windows hit a bare `OSError` stack trace when running `yantrikdb-hermes install`. The CLI now catches that and prints an actionable message:
  ```
  error: could not create symlink at <target>: <reason>
  Windows requires admin or developer-mode for symlinks. Re-run with --copy
  to install a physical copy instead:
    yantrikdb-hermes install --hermes-home <home> --copy
  ```
- **`test_install_defaults_to_user_plugin_symlink`** is now `@pytest.mark.skipif(sys.platform == "win32", ...)` so the test suite is green on Windows local dev. Linux CI (which is the gating environment) continues to exercise the symlink path.

### Credit

Thanks to [@wysie](https://github.com/wysie) for the symlink-by-default design and the test coverage. First-time external contribution; clean engineering through and through.

## [0.4.5] — 2026-05-14 — `hermes plugins install` one-command path; venv guidance

Driven by Discord question from wysie: *"can you update it so that we can easily install with hermes plugin install command? also, for the pip portion, should we be using hermes venv when installing?"* Both fair asks. Until now we shipped only the `pip install yantrikdb-hermes-plugin && yantrikdb-hermes install <hermes>` two-step. This release adds the one-command path and makes the venv expectations explicit.

### Added

- **Top-level `__init__.py` + `plugin.yaml`** at the repo root, so `hermes plugins install yantrikos/yantrikdb-hermes-plugin` lands a working memory provider end-to-end. Hermes' user-plugin loader reads the root `plugin.yaml`, sees `name: yantrikdb`, and clones the repo to `~/.hermes/plugins/yantrikdb/`. The top-level `__init__.py` dynamically loads the real plugin source from the `yantrikdb/` subfolder so the two install paths share code.

- **Hermes-loader workaround built into the top-level entry**: Hermes' user-installed-plugin loader registers the plugin module under `_hermes_user_memory.<name>` but never registers the `_hermes_user_memory` parent package. Python's import machinery then fails when our entry tries to register a child module. We pre-register a synthetic parent so the load succeeds. Forward-compatible — if Hermes fixes this upstream, our code does nothing extra.

- **README "Install in the same Python env as Hermes" guidance**: explicit instructions for `pipx` users (`pipx inject hermes-agent yantrikdb-hermes-plugin`) and standard venv users (source the venv before pip-installing).

- **Regression test** pinning the user-installed-plugin entry: simulates Hermes' loader by exec'ing the top-level `__init__.py` under a `_hermes_user_memory.yantrikdb` module name and verifies `register` + `YantrikDBMemoryProvider` are exposed.

### Notes

- The original `pip install yantrikdb-hermes-plugin && yantrikdb-hermes install <hermes>` flow is unchanged and remains the recommended path for users who already have the engine deps installed (it doesn't re-pull yantrikdb on each `pip install`).
- `hermes plugins install` does NOT auto-install pip dependencies — users still need `pip install yantrikdb` (or the plugin via pip) in Hermes' Python env afterward to get the engine.

## [0.4.4] — 2026-05-14 — Surface init failures; pre-create engine cache dir

Driven by [Issue #5](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/5) (donbowman): `hermes memory status` reported `Status: available ✓` but every tool call returned `{"error": "YantrikDB is not active for this session."}`. Root cause: when `set_embedder_named("potion-base-8M")` raised inside `initialize()` (the bundled-embedder download couldn't write to the engine's cache dir on his Hermes-sandboxed environment), the plugin caught it, logged WARNING, and returned silently — but `is_available()` still reported True because it only checks engine importability, not init success. UX trap.

### Fixed
- **Init failures are now surfaced, not buried.** When `initialize()` can't construct the backend, the error message is captured on `self._init_error` and exposed via `system_prompt_block()` so the model sees `# YantrikDB Memory — NOT AVAILABLE\nThe plugin failed to initialize: <reason>` instead of memory appearing silently absent. Logging bumped from WARNING to ERROR for backend-construction failures.
- **Engine cache dir is pre-created defensively** in `initialize()` (embedded mode only). Walks `$XDG_CACHE_HOME` then `$HOME/.cache` then `Path.home()/.cache` and `mkdir -p`s `yantrikdb/models/` under each — covers Hermes-sandboxed environments where `dirs::cache_dir()` resolves to a path the engine can't auto-create. Eliminates the `mkdir -p ~/.hermes/.yantrikdb` workaround donbowman had to discover.

### Migration
None — no behaviour change for users whose plugin was already initialising cleanly. Affects only the "what happens when init fails" path.

## [0.4.3] — 2026-05-13 — Mode-aware config schema, fixed install-doc URL

Driven by [Issue #2](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/2) (becks0815): a user followed the `Missing: YANTRIKDB_TOKEN → https://yantrikdb.com/server/quickstart/` hint from `hermes memory status`, hit broken setup commands on that page (renamed during the engine's v0.7.x refactor), and went down a Docker + token rabbit hole — when in fact the v0.2.0+ default is embedded mode and they didn't need any of it.

### Fixed
- `get_config_schema()` is now **mode-aware**. Embedded-mode users (the default since v0.2.0) only see `mode` + `db_path` + `namespace` + `top_k` in the config surface; HTTP-only `token` / `url` aren't surfaced as required-but-missing. HTTP-mode users still get the full set with `token` marked required.
- The `url` pointer on each schema entry now points at the canonical install docs in this repo's README (`#install-default--embedded-backend` for embedded, `#install-alternative--http-backend-for-ha-cluster-setups` for HTTP), not the stale `yantrikdb.com/server/quickstart/` URL that the v0.1.0 schema used.
- New `mode` entry appears first in the schema so `hermes memory setup` makes the backend choice explicit instead of defaulting to "looks like you need a token".

### Migration
None — no behaviour change for users who already have working `.env` configuration. Affects only the on-boarding UX: `hermes memory status` no longer points new users at broken docs.

## [0.4.2] — 2026-05-12 — First-class embedder loaders

v0.4.1 shipped the config surface for swapping embedders, but the only embedder-class path required users to write a thin wrapper around `model2vec` or `sentence-transformers`. That's friction the plugin should absorb — most users asking about multilingual want `potion-multilingual-128M` (a model2vec model) or one of the well-known HF sentence-transformers, both of which are one-liners to load.

v0.4.2 adds two first-class loaders so you can point at any Hugging Face model id directly, with no wrapper class to write and no `YANTRIKDB_EMBEDDING_DIM` to set (auto-probed).

### Added

- **`YANTRIKDB_EMBEDDER_MODEL2VEC`** — Hugging Face model id for the built-in `Model2VecEmbedder` loader (wraps `model2vec.StaticModel.from_pretrained`). Lightweight static-embedding family — no PyTorch dependency. Install with `pip install 'yantrikdb-hermes-plugin[model2vec]'`. Example: `YANTRIKDB_EMBEDDER_MODEL2VEC=minishlab/potion-multilingual-128M`.
- **`YANTRIKDB_EMBEDDER_HF`** — Hugging Face model id for the built-in `SentenceTransformerEmbedder` loader (wraps `sentence_transformers.SentenceTransformer`). Covers the broader HF embedder ecosystem; pulls in PyTorch. Install with `pip install 'yantrikdb-hermes-plugin[sentence-transformers]'`. Example: `YANTRIKDB_EMBEDDER_HF=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`.
- **Auto-probed dim** for the two new loaders — the plugin calls `.encode("__yantrikdb_probe__")` once during init, uses `len()` of the result as the dim, and passes that into `YantrikDB(db_path, embedding_dim=N)`. No `YANTRIKDB_EMBEDDING_DIM` env var needed for these paths.
- **Two new pip extras**: `[model2vec]` and `[sentence-transformers]`. The default install stays slim; only users who pick one of the new paths pull the heavy dep.
- **10 new tests** in `tests/test_embedded.py` covering the new loaders, auto-probe, missing-dependency actionable errors, and the extended precedence rules (151 tests total, all green).

### Behavior changes

- Path precedence is now: **CLASS > MODEL2VEC > HF > EMBEDDER (bundled-named) > default**. More-specific user intent wins: `_CLASS` is the most specific (exact Python class), the built-in loaders pick an exact HF model, `_EMBEDDER` depends on which named variants the engine version ships, and default is the fallback.
- The `[model2vec]` and `[sentence-transformers]` extras can be installed together if you want to A/B different embedders without uninstalling.
- The error message when `model2vec` or `sentence-transformers` is missing is now actionable — it points at the right pip extra by name.

### Migration

None required. With no embedder env vars set, the plugin behaves identically to v0.4.1 / v0.3.x.

### Net install for multilingual

```bash
pip install 'yantrikdb-hermes-plugin[model2vec]'  # v0.4.2
yantrikdb-hermes install ~/hermes-agent

cat >> ~/.hermes/.env <<EOF
YANTRIKDB_EMBEDDER_MODEL2VEC=minishlab/potion-multilingual-128M
EOF
```

That's the whole integration — no Python wrapper to write, no dim to look up.

## [0.4.1] — 2026-05-12 — Unblock v0.4.0 publish (lint)

Patch release: v0.4.0's tagged commit failed the publish workflow at the `ruff` gate (F841 — unused `client = ...` locals in three `tests/test_embedded.py` cases that assert against the mock instead of the returned client). PyPI never received v0.4.0; this is the first PyPI release of the pluggable-embedder feature.

### Fixed
- Removed unused `client = ` assignments in `tests/test_embedded.py` so `ruff check` passes under CI's stricter config. Pure test-code cleanup; no behavior change in the plugin.

### Note
Functionally identical to v0.4.0. Use this if you want pluggable embedders on PyPI.

## [0.4.0] — 2026-05-12 — Pluggable embedders

Lands the configuration surface for swapping the bundled embedder — driven by the first user inquiry on the repo ([Issue #1](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/1): multilingual embedding support). Default behavior is unchanged for existing users; the new env vars only matter if you want a non-default embedder.

### Added

- **`YANTRIKDB_EMBEDDER`** — name of a bundled-download embedder (e.g. `potion-base-8M`, `potion-base-32M`). The plugin calls `db.set_embedder_named(name)` on engine construction. Works with whatever named embedders `yantrikdb >= 0.7.6` ships behind the `embedder-download` feature flag.
- **`YANTRIKDB_EMBEDDER_CLASS`** — dotted Python import path (e.g. `myapp.embedders.MultilingualEmbedder`) to a class that has a `.encode(text) -> list[float]` method. The plugin imports the class, instantiates with no args, and calls `db.set_embedder(instance)`. Lets users plug `sentence-transformers`, `model2vec-rs`, multilingual variants, or any custom embedder *without* waiting on upstream bundling.
- **`YANTRIKDB_EMBEDDING_DIM`** — required when either `_EMBEDDER` or `_EMBEDDER_CLASS` is set; matches the output dim of the chosen embedder (256 for potion-base-8M, 512 for potion-base-32M, 384 for `all-MiniLM-L6-v2`, etc.). The plugin passes this to `YantrikDB(db_path, embedding_dim=N)`.
- **13 new tests** in `tests/test_embedded.py` pinning the embedder-path semantics: default (with_default) path, bundled-named path, custom-class path, dim-required-when-custom invariant, class-must-have-encode invariant, malformed-class-path errors, and class-over-name precedence when both are set.

### Behavior changes

- The plugin's embedder selection logic is now three paths instead of one:
  - `YANTRIKDB_EMBEDDER_CLASS` set → import + instantiate + `set_embedder(instance)`.
  - else `YANTRIKDB_EMBEDDER` set → construct with `embedding_dim=N` + `set_embedder_named(name)`.
  - else → `YantrikDB.with_default(db_path)` (existing v0.3.x behavior, dim=64 potion-2M).
- Class path takes precedence over named path when both env vars are set — it's the more specific instruction and doesn't depend on upstream bundling state.
- All three paths use `set_embedder*` exactly once, immediately after construction, before the engine is shared (Arc::get_mut requirement per the engine's threading contract).

### Migration for v0.3.x users

None required. With no embedder env vars set, the plugin behaves identically to v0.3.1.

### Net install for non-default embedders

```bash
pip install yantrikdb-hermes-plugin                  # v0.4.0
yantrikdb-hermes install ~/hermes-agent

# Tier 2 bundled (downloaded on first use):
cat >> ~/.hermes/.env <<EOF
YANTRIKDB_EMBEDDER=potion-base-8M
YANTRIKDB_EMBEDDING_DIM=256
EOF

# OR — custom Python embedder (e.g. multilingual, sentence-transformers):
cat >> ~/.hermes/.env <<EOF
YANTRIKDB_EMBEDDER_CLASS=myapp.embedders.MultilingualEmbedder
YANTRIKDB_EMBEDDING_DIM=384
EOF
```

### Cross-stack note

Upstream `yantrikos/yantrikdb` may add `potion-multilingual-128M` (101 languages) as a fourth named-download variant in a future release. Once that lands, multilingual users can drop the `_EMBEDDER_CLASS` Python wrapper and just set `YANTRIKDB_EMBEDDER=potion-multilingual-128M` — the plugin code is already ready for it.

## [0.3.1] — 2026-05-09 — PyPI distribution

Tooling-only release. Plugin behavior unchanged from v0.3.0 — same 8 default tools, same 3 opt-in skill tools, same feature flag, same 128 tests.

### Added

- **PyPI distribution via `yantrikdb-hermes-plugin`.** `pip install yantrikdb-hermes-plugin` installs the source under the importable package `yantrikdb_hermes_plugin` (avoids the namespace collision with the existing `yantrikdb` engine package on PyPI).
- **`yantrikdb-hermes` CLI** — bridges the pip → filesystem gap. Hermes loads plugins from `$HERMES_ROOT/plugins/memory/<name>/`, which pip can't write to directly. Two subcommands:
  - `yantrikdb-hermes install <hermes_root>` — copy the plugin source into the Hermes checkout's `plugins/memory/yantrikdb/`. `--force` overwrites an existing install.
  - `yantrikdb-hermes path` — print the on-disk path of the installed package (for users who'd rather symlink: `ln -s "$(yantrikdb-hermes path)" ~/hermes-agent/plugins/memory/yantrikdb`).
- **`.github/workflows/publish.yml`** — automated PyPI publishing pipeline triggered by tag pushes matching `v*`. Builds wheel + sdist after running ruff + mypy + pytest as a gate. Uses PyPI Trusted Publisher (no API token in repo secrets); one-time config on PyPI's web UI.

### Net install flow (post v0.3.1 publish)

```bash
pip install yantrikdb-hermes-plugin           # the plugin source + CLI
yantrikdb-hermes install ~/hermes-agent       # copy into plugins/memory/
hermes config set memory.provider yantrikdb
echo "YANTRIKDB_MODE=embedded" >> ~/.hermes/.env
```

`yantrikdb` (the engine, ~10 MB with bundled embedder) is pulled automatically as a dependency.

### Internal

- `yantrikdb/__init__.py` now wraps `from agent.memory_provider import MemoryProvider` and `from tools.registry import tool_error` in try/except so the package imports successfully outside a Hermes runtime (e.g. when the CLI invokes `from yantrikdb_hermes_plugin.cli import main`). Stub `MemoryProvider` and `tool_error` are used in that path; they're never the ones Hermes sees because Hermes loads the plugin via fresh filesystem import from `plugins/memory/yantrikdb/`.

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
