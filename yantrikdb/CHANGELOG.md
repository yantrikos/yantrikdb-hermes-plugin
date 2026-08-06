# Changelog

All notable changes to the YantrikDB Hermes memory plugin.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); semantic versioning. Distributed standalone per Hermes maintainer guidance (PR #9989 closed 2026-05-13).

## [0.15.0] — 2026-08-06 — Better retrieval, and one feature switched off on the evidence

Two changes. One gives you a retrieval improvement that was sitting behind a version pin. The other **turns off a feature that has been quietly telling you nothing**, and explains why in enough detail that you can check the claim yourself.

### Engine 0.13.x is now supported — you get BM25 lexical fusion

The ceiling was `<0.13.0`, so nobody installing this plugin received the engine's new lexical fusion. It now admits `<0.14.0`, verified against the full suite on 0.13.0 before the bound moved — which is the rule `test_dependency_pins.py` exists to enforce. The bound moves; it never disappears.

What fusion buys, measured on a deliberately hostile 4,353-record corpus: **precision@5 0.25 → 0.30 for +0.5 ms**, and two answers that previously never appeared in the top 100 at all became reachable. On corpora where records don't all share a sentence frame, the engine team measured considerably more.

### Knowledge-gap detection is now OFF by default

Since v0.10.0 this plugin has watched for questions your memory answers badly and turned recurring ones into agenda items and to-dos. **It doesn't work, and this release stops pretending it does.**

`gap_max_avg_top_score` compares a *composite* recall score against a threshold. That composite folds in importance, recency and decay — terms that describe how to weight a result **once you've decided to return it**, not whether memory holds anything near the question. Measured on engine 0.13.0 over 4,353 records:

```
queries flagged as a gap ................ 0 / 20
deliberate nonsense NOT flagged ......... 3 / 4
  "zzqx wobble frangible" ....... avg 0.4810
  "quantum tarpaulin metric" .... avg 0.3707
  "grommet fitzwilliam parsnip" . avg 0.3023
```

Gibberish scores as high as a real question. So the detector is not being cautious, as v0.12.2 intended when it recalibrated the threshold — **it is uninformative, and its silence reads as "nothing is missing."** A signal that cannot be wrong cannot be right either.

**The defect is the instrument, not the number.** v0.12.2 already retuned this constant once; retuning it again would move the same bug to a new value and expire it at the next scoring change. So the default goes off until the signal itself discriminates.

Deliberately **not** replaced with a similarity threshold: on the same corpus nonsense reached `max_similarity` 0.653 against 0.624 for real queries, so that swap is unproven and might only relocate the defect. The engine team is gating a `min_similarity` candidate on evidence; this plugin adopts it if and when it separates.

Nothing was removed. `YANTRIKDB_GAP_DETECTION=true` restores the previous behaviour, and it appears in `hermes memory setup` with the risk stated rather than the mechanism. **Open tasks are unaffected** — those are facts you wrote, not a signal we inferred.

### Check it against your own memory

```bash
python benchmarks/gap_floor_check.py <your.db> [--threshold 0.30]
```

Reports the observed score distribution, probes it with nonsense, and exits non-zero if gibberish clears your threshold. If you calibrated a threshold against a composite score anywhere, this tells you in one command. It deliberately does **not** compute a "floor" from the per-component contributions — those don't sum to the composite, and an earlier version of this analysis drew a wrong conclusion from assuming they did. The script says so, so nobody repeats it.

### The benchmark gate is now in the repo

`tests/comparison/gate_4k.py` plus hash-pinned fixtures — the competing-distractor gate used to evaluate engine candidates, including a direction-sensitivity subset and a possessive minimal pair. It refuses to run on a corpus whose hash has drifted, and CI asserts the hashes so a regenerated fixture can't silently make old numbers incomparable. It is a **stress** corpus, pathological by construction; weight a production-clone benchmark above it for any user-facing claim.

19 new tests; 456 pass / 3 skip.

## [0.14.0] — 2026-08-05 — Standing rules that belong to the operator

Until now exactly one channel in this plugin carried always-injected **rules** rather than facts: a knowledge pack's constitution. Which meant **a third party could install standing rules into someone's agent and the person who owns it could not.** That asymmetry was the wrong way round.

### `yantrikdb-constitution.md`

Write your rules in a markdown file — `$HERMES_HOME/yantrikdb-constitution.md`, or set `YANTRIKDB_CONSTITUTION_PATH`:

```markdown
- Never run destructive commands without asking.
- Answer in British English.
- Never reveal internal hostnames.
```

They are injected **first**, and stated to outrank recalled memory, mounted packs, and everything else in the prompt.

### Four properties that make it a guardrail rather than another block

- **It never yields to the adaptive budget.** Every other injected block scales down as the context window fills; this one is bounded but never shrunk. A nearly-full window is exactly when an agent starts cutting corners, and a rule that disappears under pressure was never a rule.
- **It survives an unavailable backend.** Writing the tests caught a real bug here: the memory-unavailable path returned early, so rules vanished precisely when memory was broken. Fixed — rules are prepended to every return path. Guardrails stored inside the thing they constrain are not guardrails.
- **The agent cannot edit it.** There is deliberately no tool. A tool to rewrite standing rules is exactly the capability an agent must not have; the file is the interface and the operator is the editor. A test asserts no tool mentions it.
- **Truncation is loud.** An oversize file is capped and logs a warning naming what was dropped — silently trimming rules would leave an operator believing rules are in force that are not.

### Also
`constitution_path` now appears in `hermes memory setup`, described by what it is for.

**Not** included: the engine's personality/persona surface (`derive_personality`, `set_personality_trait`) is still deliberately unused — `think()` continues to pass `run_personality=False`. Exposing it is a separate decision about whether an agent's manner should drift from a transcript, and it deserves its own opt-in rather than arriving inside a guardrails release.

10 new tests; 437 pass / 3 skip.

## [0.13.0] — 2026-08-05 — Built for fleets, not single agents

Hermes users run **N agents**, not one. This release makes the plugin legible to an operator running twenty of them.

### The fleet was already isolated — and invisible

Each agent gets `{base}:{workspace}:{identity}` automatically, so nothing contaminates anything else. Verified under real load: **6 separate processes writing one embedded database, 720 concurrent writes — 0 errors, 0 backpressure, 0 namespace leakage**, each agent reading only its own memories.

But that isolation meant every surface here was single-agent. An operator could not ask *"what is my fleet working on"*, *"which agent is stuck"*, or *"has another agent already learned this"*.

**New `yantrikdb_fleet` tool** (opt-in, `YANTRIKDB_FLEET_VIEW=true`) reports each sibling agent's memory count, last activity, and open task count — read-only.

Its boundaries matter more than its output, so they are the tests:
- **Refused entirely when `owner_scoping` is on.** Under owner scoping sibling namespaces are *people*, not agents; a "fleet overview" that quietly enumerated a user's family would be the exact identity-contamination failure that scoping exists to prevent.
- **Never crosses workspaces** — siblings are agents of the same `{base}:{workspace}`, not everything sharing a tenant prefix.
- **Truncation is declared.** A capped scan reporting itself as complete would be a silent lie about coverage.
- **An unreachable sibling reads as unknown, never as zero open tasks.**

### Cross-agent learning is findable now

`shared_brain_namespace` has existed since v0.5 — set it and what one agent is explicitly told to remember becomes recallable by all of them, tagged with which agent contributed. It was in no config schema at all, so `hermes memory setup` never mentioned it and an operator running a fleet would never discover it existed. Both it and the fleet view are now described in `hermes memory setup` by the problem they solve.

### Known gap, raised upstream

Packs are embedded-only, so the fleet's best deployment (one shared `yantrikdb-server`, agents isolated by namespace) currently **cannot mount a pack** — an operator picks isolation-and-scale or attachable expertise, never both. That needs pack endpoints on the server; requested from the engine team with the surface and the two design constraints that matter.

8 new tests; 427 pass / 3 skip.

## [0.12.2] — 2026-08-04 — Recalibrate the gap threshold for engine 0.12.1

Engine 0.12.1 changed how recall scores are composed, and one of our defaults silently inverted because of it. Nothing crashed, no test failed, and the whole suite stayed green — every test asserted *behaviour*, none asserted *calibration*.

**What changed upstream.** Recency used to **add** up to +0.5 to every fresh record; it now **multiplies** relevance, bounded to +12.5%. Retrieval got better — on our benchmark corpus MRR rose 0.928 → 0.946 and recall@1 0.865 → 0.892 — but absolute scores roughly halved: avg-top-3 median **1.138 → 0.510**.

**Why that mattered here.** `gap_max_avg_top_score` is an absolute score, so it only means what it meant while the engine composes scores the same way. Measured on the same corpus, the old 0.5 default flagged **0/37 queries as knowledge gaps on 0.11.3 and 17/37 on 0.12.1** — and the self-directing loop has been on by default since v0.10. A user upgrading their engine would have found an agenda filling up with "resolve knowledge gap" tasks for questions their memory answers correctly at rank 1.

**The new default is measured, not guessed.** 0.30 flags 0/37 well-answered queries while still catching genuinely unanswerable ones. The distributions overlap (answered min 0.320, unanswerable max 0.441), so no threshold separates them cleanly; this errs toward missing a gap rather than inventing one, because a missed gap costs nothing visible while a false task costs the user's attention. The recurrence requirement (`gap_task_min_count`, default 3) remains the second filter behind it.

**`auto_recall_min_score` was left at 0.4** — deliberately. The obvious move was to scale it down with everything else, but measuring said no: at 0.4 it drops 0/37 relevant hits, and lowering it only admits more noise.

**Engine floor raised to `>=0.12.1`.** One absolute threshold cannot serve both scoring scales — 0.30 would go nearly silent on 0.11.3, and 0.5 mints junk on 0.12.1. Rather than ship a default that is wrong on one of them, the floor moves to the line the calibration was measured against.

New `tests/test_gap_calibration.py` re-measures the default against the benchmark corpus and fails if it starts flagging answerable questions as gaps. Verified it rejects the old value (46% → FAIL) and accepts the new one (0% → pass), because a guard that cannot fail is not a guard.

## [0.12.1] — 2026-08-04 — Bounded dependencies, and engine 0.12.0

### Every dependency now has a ceiling

Both runtime dependencies were unbounded (`yantrikdb>=0.11.3`, `requests>=2.31`), as were the embedder extras. An unbounded `>=` lets a future major **break releases that are already published**: nothing in this repo changes, upstream cuts a new major, a fresh `pip install` resolves into it, and the failure looks like our bug. There is no signal — no CI run, no diff, no notification — because from our side nothing happened.

Not hypothetical. `yantrikdb-mcp` lost their published v0.10.0 exactly this way when the MCP SDK shipped 2.0.0 against `mcp[cli]>=1.2.0`; the release re-shipped itself broken and they flagged it ecosystem-wide. This plugin has also moved its engine floor twice for defects a ceiling would have contained.

Now: `yantrikdb>=0.11.3,<0.13.0`, `requests>=2.31,<3.0.0`, `model2vec>=0.3,<1.0.0`, `sentence-transformers>=2.7,<6.0.0`. New `tests/test_dependency_pins.py` enforces it — including that both `plugin.yaml` manifests match `pyproject.toml`, since a ceiling present in one install path and absent in the other protects only half the users. **A ceiling is a claim about tested ground**; raise one only after running the suite against the new major, which is what happened here.

**On the MCP SDK specifically:** this plugin does not use it. Verified by grep across all shipped code and both manifests — the plugin talks to Hermes through its `MemoryProvider` interface, not MCP, so the 2.0.0 breakage cannot reach it.

### Engine 0.12.0 verified, ceiling set to admit it

Full suite passes **unmodified** against 0.12.0 (417 pass / 3 skip), and recall still returns every field the plugin reads.

0.12.0 also **closes an API gap this plugin reported**: recall results now carry `metadata` (plus `namespace`, `source`, `current_status`, `superseded_by`). Provenance stamps were previously write-only on the path agents actually use — readable via `list_records`, absent from `recall`.

### Knowledge-pack hits are marked in recalled memory

Now that recall reports `namespace`, a hit that came from a mounted pack is labelled *"from a knowledge pack — third-party, not your memory"*. Since v0.11 a pack's records are recallable alongside the agent's own, and rendered as identical bullets the agent could not tell a stranger's claim from something the user told it — the same conflation v0.12.0 fixed at the block level, one layer deeper. Nothing is labelled when no pack is mounted, so the distinction costs no tokens where it cannot apply.

## [0.12.0] — 2026-08-03 — What always-on agents actually needed

Three fixes from researching how Hermes is really used — its community story set (237 entries, 45 of which mention memory), its issue tracker, and the v0.15.1 source. Each closes a gap that looked fine from inside this repo.

### Memory now consolidates mid-session

Consolidation *and* the self-directing gap→task loop both hung off `on_session_end` — which Hermes fires only at **real session boundaries**: CLI exit, `/reset`, gateway session expiry. Never per turn; the source says so explicitly.

The community runs agents continuously: a Raspberry Pi on 24/7, Telegram and Discord gateways, always-on assistants. Those sessions can go hours or days without a boundary — so the substrate's background work **never ran for exactly the deployments accumulating the most to consolidate**, and the self-directing loop we turned on by default in v0.10 sat idle for them. Upstream has asked for this twice under the name "dreaming" ([#10771](https://github.com/NousResearch/hermes-agent/issues/10771), [#25309](https://github.com/NousResearch/hermes-agent/issues/25309)).

The same pass now also runs on a cadence: after `maintenance_cadence_turns` turns (default 40), provided `maintenance_min_interval_seconds` (default 1800) have elapsed. **Both conditions are required** — turns alone would fire during a burst of rapid messages, which is the moment least worth interrupting; elapsed time alone would fire on an idle session with nothing new. It runs in a background thread, never two at once, and never on the turn's critical path. Set `YANTRIKDB_MAINTENANCE_CADENCE_TURNS=0` for the old session-end-only behaviour.

Verified on a live install: with the session never ending, `periodic think` fired at turns 5 and 10 and the gap→task loop ran with it.

### Recalled memory no longer arrives wearing the user's authority

Hermes injects prefetch output into the **current turn's `role: user` message** ([#31584](https://github.com/NousResearch/hermes-agent/issues/31584)), so unlabelled recall is indistinguishable from what the person just typed. That is a correctness problem — the agent "remembers" the user saying things they never said — and a prompt-injection surface, since anything that ever reached memory arrives carrying the user's authority. v0.11's packs sharpened it: third-party pack content could reach the prompt the same way.

Recall is now framed as background reference rather than user speech. The label is deliberately one line — **83 characters, ~20 tokens** — because it is fixed overhead on every turn and v0.10 was spent cutting exactly that kind of cost.

### Per-user isolation is findable by the people who need it

Four upstream issues describe the same pain, [#11430](https://github.com/NousResearch/hermes-agent/issues/11430) most sharply: in group chats a shared memory makes the agent attribute one user's facts and preferences to another, which *"breaks user trust"*.

The plugin has had the fix since v0.4 — `owner_scoping` gives each resolved owner its own namespace — but `hermes memory setup` described it as *"append a stable resolved-owner shard to the namespace… without requiring YantrikDB core provenance columns"*, which is unsearchable by someone whose actual problem is "my agent thinks my wife is me". The setup description now names the symptom, the situations it applies to, and what switching it on changes. (Still off by default: it changes where new memories are written. Existing ones stay readable via `include_base_namespace_recall`.)

12 new tests; 408 pass / 3 skip.

## [0.11.0] — 2026-08-02 — Attachable expertise (knowledge packs)

A **pack** is a sealed, signed knowledge file: mount it to gain its knowledge and rules, unmount to give them back leaving your own memory byte-for-byte as it was. Engine 0.11.0 added the primitive; this release makes a Hermes agent able to use it. No other Hermes memory provider can attach expertise.

- **New tool `yantrikdb_packs`** — `list` (mounted + installed, and flags any installed pack that failed to re-mount), `inspect` (read a pack's manifest **without** mounting it), `mount` / `install`, `unmount` / `uninstall` / `unmount_all`. A bare filename resolves against the engine's pack directory, so an agent needn't know where the host keeps packs.
- **Rules injected into the system prompt.** `pack_context()` — each mounted pack's coverage index and constitution — is assembled *by the engine* so every consumer injects identical text; we pass it through verbatim, capped by `pack_context_max_chars` and scaled by the v0.10 adaptive budget. Attached expertise is worth nothing if it crowds out the conversation that needed it.
- **Knowledge actually reachable.** A pack's records live in the namespace its author sealed them under, while recall is scoped to the agent's own namespace — so a naive implementation delivers the pack's *rules* and none of its *knowledge*, with every surface still looking healthy. Recall is now widened into the namespaces of mounted packs, resolved precisely from each pack's manifest rather than by recalling unscoped (which would also expose every other namespace in the database).
- **Auto-mount is transient.** `YANTRIKDB_AUTO_MOUNT_PACKS` mounts on `initialize` and unmounts on `shutdown`, because `mount` never writes to the database and a session that opens with packs should leave nothing behind. `install` — the durable counterpart — stays an explicit, deliberate action. One unreadable or embedder-mismatched pack is logged and skipped rather than taking memory down for the session.
- **Refusals are reported, not forced.** Mounting is refused when a pack's vectors are provably from a different embedding space; the tool description tells the agent to report that rather than retry with `allow_unverified_embedder`, because the refusal is what prevents confidently wrong answers.
- **HTTP mode refuses honestly.** Packs are an embedded capability — the database lives on the server there, and `yantrikdb-server` exposes no pack endpoints. The client says so instead of returning an empty list, which would read as "no packs are mounted" — a different and misleading claim.

**Off by default** (`YANTRIKDB_PACKS_ENABLED=true`): mounting somebody else's knowledge is a decision an operator makes, not one they discover. Like skills, the flag is orthogonal to `tool_profile` — enabling packs and then being unable to reach them because the profile is `core` would be a trap.

Verified end-to-end against engine 0.11.3 with a real sealed pack: recall for a pack topic returned **0 hits → mount → 3 hits** with the constitution injected → **unmount → 0 hits** and the block gone. 20 new tests; 396 pass / 3 skip.

## [0.10.1] — 2026-08-01 — Require the engine release that can't lose recall

Pin raised to **`yantrikdb>=0.11.3`**. No plugin behaviour changes; the full suite passes unmodified against 0.11.3 (376 pass / 3 skip) and every engine method this plugin calls is unchanged.

The floor moves because engine 0.11.3 fixes a defect in the vector index that could **accept a write and then make the record unreachable by every possible query — including a search for its own exact text**. The victim changed on each index build (graph construction seeds from entropy), so the same database could serve a different subset each time it opened. Upstream's summary is blunt about the blast radius: *"Any deployment on an earlier version can be holding memories it cannot return."*

Our previous floor (`>=0.10.1`) allowed a fresh install to resolve to an affected engine, which meant a user could store a memory successfully and later fail to recall it, with nothing anywhere reporting a problem. Raising the floor closes that window rather than documenting it.

**No migration.** The index rebuilds from SQLite on open and the repair runs there, so an affected database heals itself the first time it is opened on 0.11.3. If yours was affected you'll see `vec index rebuild reconnected unreachable nodes` in the logs, and the count is how many records were rescued.

## [0.10.0] — 2026-07-25 — The install is the product

What you get by running `pip install` should be what the plugin is actually *for*, it shouldn't cost more context than it's worth, and it should use what Hermes actually offers.

### Delegated work is remembered (`on_delegation`)

Hermes delegates to sub-agents and calls `on_delegation(task, result, child_session_id)` when one returns. **No Hermes memory provider implements that hook.** So today a sub-agent investigates something, reports back, its session ends — and the finding exists only in the transcript. The parent remembers *having delegated*; it can't recall what came back, and the next session has nothing.

The plugin now stores the `(task, result)` pair as an **episodic** memory in the **parent's** namespace, written at importance 0.65 — a delegation is by construction work someone thought worth spinning up an agent for. Each field is bounded by `delegation_max_len` (default 4,000 chars) so a verbose sub-agent can't flood the substrate, and the hook is fail-soft and circuit-breaker aware. Disable with `YANTRIKDB_CAPTURE_DELEGATIONS=false`.

Verified end-to-end on a live install against engine 0.10.1: the delegated finding is written and comes back through `yantrikdb_recall` on the parent. Provenance (`source=hermes_delegation`, `child_session_id`) is stamped in metadata and confirmed stored — though note that **`recall` results do not currently carry metadata** (it is readable via `list_records`), so within a recall the marker a reader actually sees is the record's own `Delegated task: … / Result: …` shape. Raised with the engine team as an API gap; the stamps are durable either way.

Verified first-hand against a real **Hermes v0.15.1** install: the signature matches `MemoryProvider.on_delegation` exactly, and `MemoryManager` invokes it from `tools/delegate_tool.py` when a subagent completes. Hermes' own docstring confirms the design — *"Called on the PARENT agent when a subagent completes… The subagent itself has no provider session"* — so a finding nobody captures here is genuinely unrecoverable.

Note for anyone reading our manifests: the `hooks:` list in `plugin.yaml` is **documentation only**. Hermes parses `provides_hooks` (a different key, for the separate plugin-hook system — `pre_tool_call`, `on_session_start`, …), and memory-provider lifecycle methods are called directly on the provider object regardless of what any manifest says. An earlier draft of these notes claimed the declaration was load-bearing; inspecting Hermes disproved it.

### The self-directing loop is ON by default

`YANTRIKDB_AUTO_GAP_TASKS` and `YANTRIKDB_SURFACE_AGENDA` now default to **true**. Through v0.9.x they shipped opt-in, which meant the capability this plugin is known for — memory that notices what it can't answer, queues the work, and opens the next session with its own agenda — never ran for anyone who installed it and didn't read the README. The default install was a competent memory store and nothing more.

Both halves were already bounded, which is why turning them on is safe rather than merely bold: new tasks are capped per session (`gap_task_max`, default 3), the agenda block is capped in lines (`agenda_max_items`, default 5), and the gap gate is **demand-aggregated** — a query must recur *and* keep scoring poorly before it becomes a task, so a single low-confidence recall can never mint one. Set either env var to `false` to restore the pre-0.10 behaviour.

### Adaptive prompt budget (`on_turn_start`)

Hermes passes `remaining_tokens` on every turn, and no memory provider uses it. Everything this plugin injects — recall hits, skill bodies, conflicts, hygiene, the verbatim buffer, the agenda — competes with the conversation for one window, and memory is most expensive exactly when that window is nearly full. A fixed budget is wrong in both directions: wasteful early, harmful late.

The optional blocks now taper between a high watermark (full injection) and a low one (essentials only). Measured on a live install:

| host signal | scale | injected block |
|---|---|---|
| **none** (older Hermes / kwarg omitted) | 1.00 | **1,188 chars — byte-identical to pre-0.10** |
| ~120k remaining | 1.00 | 1,188 chars |
| ~20k remaining | 0.29 | 912 chars |
| ~5k remaining | 0.00 | 716 chars |

Two properties matter more than the taper. **Unknown means unchanged** — absent a signal we behave exactly as before, because guessing "probably tight" would silently degrade hosts that were working fine. And **a live budget never rounds down to zero**: while any budget remains a block keeps at least its most relevant entry, so it degrades visibly instead of vanishing unexplained.

The hook does no backend work; it runs every turn, and a provider that spends the turn's first milliseconds on maintenance is one people disable. Opt out with `YANTRIKDB_ADAPTIVE_PROMPT_BUDGET=false`; tune via `YANTRIKDB_PROMPT_BUDGET_HIGH` / `_LOW`.

### Tool profiles — half the per-turn context

New `YANTRIKDB_TOOL_PROFILE`, defaulting to **`core`**:

| profile | tools | schema bytes | ≈ tokens/turn |
|---|---|---|---|
| **`core`** (default) | **7** | 6,900 | **~1,725** |
| `full` | 18 | 14,426 | ~3,606 |

Tool schemas are re-sent on **every request**, so the surface is a running per-turn tax, not a one-off. At 18 tools this plugin billed ~3.6k tokens/turn — against 2–5 tools for every other Hermes memory provider — and a wide surface also degrades selection, since near-synonymous tools invite the wrong pick.

`core` is `remember` / `recall` / `forget` / `relate` / `conflicts` / `resolve_conflict` / `tasks` — what an agent reaches for mid-conversation, plus the two surfaces that make this substrate different from a vector store. **Nothing is disabled.** The excluded tools cover work that already happens without the model spending a turn on it: `think()` runs automatically at session end, conflicts and hygiene surface in the system prompt, gaps surface in the agenda, and stats/observability/trigger tools are operator diagnostics. They remain fully dispatchable, and `YANTRIKDB_TOOL_PROFILE=full` exposes them all. Skills are orthogonal — `YANTRIKDB_SKILLS_ENABLED=true` surfaces them in either profile, because that flag is itself the opt-in.

A test pins the ratio, so adding a tool to `core` later can't quietly undo the saving.

## [0.9.3] — 2026-07-25 — Share one embedded engine per database

Cuts per-agent resource cost in embedded mode, and requires the engine release that fixes the idle-CPU defect behind a field report on a Ryzen 9950X3D (16C/32T), where an independent memory-dump audit measured the Hermes backend at **~55% of a 32-logical-processor machine while idle**, with ~600k read ops/sec inside compiled YantrikDB threads.

- **Engine requirement raised to `yantrikdb>=0.10.1`.** The root cause of that report was engine-side ([#113](https://github.com/yantrikos/yantrikdb/issues/113), fixed in 0.10.1), so the pin moves rather than leaving users able to install an engine that still carries it.

Hermes constructs a memory provider per agent/session, and every one resolves to the same database (`$HERMES_HOME/yantrikdb-memory.db` unless `YANTRIKDB_DB_PATH` says otherwise). Before this release each provider opened its **own** engine over that same file, and every engine spawns its own materializer workers plus a compactor — so a host running N agents paid N times for background work on one database, and those workers poll whether or not anything is happening.

- **One engine per (database + embedder), process-wide.** `EmbeddedYantrikDBClient` now reuses an already-open engine instead of constructing a fresh one. The cache key includes the full embedder selection, because the embedder fixes vector dimensionality — configurations that disagree never share a handle. A cache hit is checked *before* embedder materialization, so it also skips re-loading the model on the `model2vec` / `sentence-transformers` paths.
- **Opt out** with `YANTRIKDB_SHARE_ENGINE=false` to restore per-provider engines.
- Cached engines are intentionally never evicted — they mirror process lifetime, matching the previous behaviour where each provider held its engine until interpreter shutdown.

**On the required engine (0.10.1), this is a resource fix, not a CPU fix** — and that distinction is deliberate. Measured on a 32-logical-CPU box, 6,000 records, 6 providers, idle, sampling gated on the materializer having drained (`oplog WHERE applied = 0` at zero) rather than on a fixed sleep:

| engine | 6 providers, engine each (≤0.9.2) | 6 providers, shared (this release) |
|---|---|---|
| **0.10.1+** (required; #113 fixed) | 137 threads, 0.16% of machine | 52 threads, **0.05% of machine** |
| 0.10.0 (pre-#113, no longer supported) | 152 threads, 31.9% of machine | 52 threads, 3.7% of machine |

The bottom row is why the pin moved: sharing an engine looked like a large CPU win only because each extra engine multiplied an engine-side defect. With that fixed the CPU difference is near the noise floor, and the benefits that remain are the resource ones — **~85 fewer OS threads**, N× fewer SQLite connections and file handles, and no duplicate embedding-model load per agent (costly on the `sentence-transformers` path). Those are worth shipping on their own terms; the headline was rewritten rather than kept.

**On the engine defect itself.** The materializer polled every 100 ms for unapplied operations using an index declared only in a schema migration and never in the base schema — so every database *created* after that migration never had it, and each poll fell back to walking the whole oplog. Idle cost grew superlinearly with operation count (worker count held constant: 500 records → 1.25% of machine, 2,000 → 4.65%, 6,000 → **34.41%**), and at depth it starved real ingest (`Backpressure: ingest queue full`). Reported upstream with a reproduction harness; fixed in engine 0.10.1 and verified here at **246× lower idle CPU** at 6,000 records with backpressure events going 7 → 0.

The harness that measured all of this ships in [`benchmarks/idle_cpu_bench.py`](../benchmarks/idle_cpu_bench.py) and is run by the engine team too, so a regression is caught by the same instrument on both sides.

## [0.9.2] — 2026-07-19 — Fix embedded package-name collision (issue #50)

Fixes a real, high-severity bug reported by [@AtheIIa](https://github.com/AtheIIa) in [#50](https://github.com/yantrikos/yantrikdb-hermes-plugin/issues/50): embedded mode could silently fail to initialize because this plugin's own top-level package is named `yantrikdb` — identical to the engine it depends on. When the plugin directory wins `sys.path` resolution (the `hermes plugins install` layout, or any run from a source checkout), `from yantrikdb._yantrikdb_rust import YantrikDB` bound to the plugin (which has no `_yantrikdb_rust`) and raised `ModuleNotFoundError`, surfaced misleadingly as "requires yantrikdb >= 0.7.4" even though the engine was installed and importable on its own. The `pip install yantrikdb-hermes-plugin` path was never affected (setuptools imports it as `yantrikdb_hermes_plugin`).

- **Layer 1 — collision-proof engine load.** `embedded.py` now loads the engine through `load_engine_yantrikdb_class()`: a fast-path plain import first, and on failure it locates the real `yantrikdb` **distribution** (unambiguous — the plugin's distribution is `yantrikdb-hermes-plugin`) via `importlib.metadata`, loads its `_yantrikdb_rust` extension directly regardless of `sys.path` order, and caches it so later plain imports resolve the engine too. Errors are now truthful: "engine not installed" vs. "engine installed but shadowed — install via pip to avoid the collision."
- **Layer 2 — no more silent success.** When init genuinely fails (`_client is None`), a dropped `memory add` mirror is now **loud and counted** (`ERROR` logged once, `_dropped_writes` tally) instead of a silent no-op — the reporter's agent had no in-band signal that persistence was broken and confabulated successful writes. `is_available()` now reports the truth under the shadow (locates the engine without importing the shadowed name), and the NOT-AVAILABLE system-prompt block explicitly instructs the model **not** to claim memories were saved and names the collision as a likely cause.
- The permanent rename (`yantrikdb` → `yantrikdb_hermes_plugin`) that would eliminate the collision class entirely is deliberately deferred to a later minor: it changes the `hermes plugins install` registration path and is breaking for existing installs.
- 8 new CI-safe tests (`tests/test_issue50_collision.py`) simulate the shadow without a native engine; suite 339 pass / 3 skip.

## [0.9.1] — 2026-07-19 — HTTP idempotency: capability-gated key forwarding

Completes the idempotency story for the **optional HTTP backend**. (The default embedded/core path has had idempotent `remember` since v0.9.0 — this only affects `YANTRIKDB_MODE=http` against a `yantrikdb-server`.) Coordinated with yantrikdb-server, whose PR #67 shipped the endpoint with the conflict as **HTTP 200** so it stays byte-identical to the embedded surface.

- **Capability-gated forward (agreement #6).** In http mode, `remember(idempotency_key=…)` now probes `/v1/health` once (cached) for an advertised `idempotency_key` capability. Present → the key is forwarded and the server's response flows through unchanged (same-key/same-text → original rid; same-key/divergent-text → `200 {stored:false, idempotency_conflict:true, rid}`). Absent, or the probe can't confirm → the plugin still **refuses loudly** — never forward-and-silently-drop. Handles both capability shapes (list of features or `{feature: bool}` map).
- A follower-write leader-redirect (503) maps to a transient via the existing error taxonomy. Auto-following the leader is out of scope (the http client is single-endpoint; point it at the leader or a load balancer).
- New live integration case (`tests/integration/test_live.py`, gated on `YANTRIKDB_INTEGRATION_URL`) validates dedup zero-writes + divergent-conflict against a real server; feature-probed, skips when the capability is absent.

No engine pin change (`yantrikdb>=0.10.0`). Mock-covered for the capability-gate logic; ruff + mypy clean.

## [0.9.0] — 2026-07-17 — Idempotent writes, typed errors, and a contract gate

Built with the yantrikdb ecosystem (core / server / mcp) on engine **0.10.0 "the Reliability release"**, and the reason the plugin now **requires `yantrikdb>=0.10.0`** (pin bumped). Three additive pieces; existing behaviour unchanged.

### Idempotent `remember` (NEW, opt-in)

- `yantrikdb_remember` accepts an optional `idempotency_key`. In embedded mode the keyed write routes through the engine's `record(idempotency_key=…)` (drift-safe digest — the engine vector is excluded, so an embedder upgrade between retries can't fake a conflict). **Same key + same text → the original rid with zero writes; same key + different text → a conflict carrying the existing rid** (surfaced to the agent as claim resolution, `stored:false` — fetch/correct it, don't re-store). Derive keys from stable EXTERNAL identity (a message id), never from the text.
- **Honest refusal** (ecosystem agreement #6): keys are refused loudly, never silently dropped, in http mode (server endpoint not shipped) and on python-fallback embedders (`model2vec` / `sentence-transformers`), which can't produce the drift-safe digest — the error names the backend and the fix.

### Typed error taxonomy (NEW)

- `_map_engine_error` now branches on the engine's 0.10 **typed exceptions** (`Backpressure` / `RecallContended` / `CorrectionDeferredDuringReembed` / `BatchDeferredDuringReembed` → transient/retryable; `IdempotencyConflict` / `InvalidIdempotencyKey` / `ProvenanceInconsistent` → caller-actionable) — by **type, never message text**. Falls back to the prior string heuristics on engines that don't export them.

### Consumer-simulation contract gate (NEW)

- `tests/test_semantic_contract.py` — a semantic gate seeded through the public tool surface, all-or-nothing, **feature-probed (never version-parsed)**. Cases: namespace isolation, **knowledge_gaps namespace-scoping (encodes the 0.9.3 break so it can never silently regress)**, idempotency dedup (zero-writes) + divergent conflict, verbatim/injection fidelity, and explainable-recall survival. Design ported from yantrikdb-mcp's contract suite (thanks). Skips cleanly without the native engine wheel.

324 tests pass on engine 0.10.0; ruff + mypy clean; no new dependencies.

## [0.8.1] — 2026-07-17 — Fix: knowledge_gaps is namespace-scoped on engine 0.9.3+

Bug fix. Engine **0.9.3** made `knowledge_gaps` namespace-scoped (demand is now recorded per namespace, and disabled entirely on encrypted DBs — a privacy fix). The plugin called it without a namespace, so on any engine **≥0.9.3** it queried the wrong (`default`) namespace and returned nothing — leaving the **`yantrikdb_knowledge_gaps` tool and the entire v0.8 self-directing loop (gap→task, "your memory's agenda") silently dormant.**

- `knowledge_gaps` (HTTP + embedded) now accepts and passes `namespace`; the three call sites (`_do_knowledge_gaps`, `_auto_gap_tasks`, `_format_agenda_block`) pass the active namespace.
- Backward-compatible: engines 0.9.0-0.9.2 have no `namespace` parameter (demand was global there) — the embedded path falls back to the unscoped call on `TypeError`.
- Verified on engine 0.10.0: with the fix, `knowledge_gaps` returns this namespace's gaps and the self-directing loop fires again. Full suite (312 tests) + benchmark green on 0.10.0; no engine pin change.

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
