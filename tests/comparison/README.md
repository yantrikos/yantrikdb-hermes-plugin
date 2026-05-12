# Memory provider comparison harness

A reproducible side-by-side test harness for the 9 Hermes memory providers (this plugin + the 8 that ship with Hermes). Each provider is installed against a real Hermes instance, exercised with the same canonical inputs, and its `recall()` response shape is captured as a structured fixture so the comparison table in the parent `README.md` is backed by code anyone can re-run, not derived from `plugin.yaml` marketing copy.

## Why this exists

The README originally had a "vs other providers" comparison table built from each provider's `plugin.yaml` description. That's not enough — a provider could implement canonicalization, contradiction tracking, or explainable recall without advertising it. Claiming "YantrikDB has X and the others don't" based only on what they say about themselves is strawmanning, not comparing.

This harness replaces that comparison with verified, reproducible findings. The table in the main README is generated from `findings.yaml` files produced by running each provider against the same fixtures.

## What gets exercised

For each provider, the harness drives the **MemoryProvider ABC contract** (the same surface Hermes uses):

1. **`initialize(session_id)`** — does the provider load cleanly with minimal/default config?
2. **`handle_tool_call("<provider>_remember", {...})`** — store a canonical fact.
3. **`handle_tool_call("<provider>_recall", {...})`** — retrieve it back; capture the raw response.
4. **Duplicate write** — store the same fact a second time; observe whether the provider canonicalizes or pushes a new record.
5. **Contradiction write** — store a fact that contradicts an earlier one; observe whether the provider surfaces a conflict, silently overwrites, or stores both.
6. **Tool schema introspection** — call `get_tool_schemas()` and check for skill-related tools (`*_skill_define`, `*_skill_search`, `*_skill_outcome` or equivalents).
7. **Response field introspection** — does the recall response contain a `why_retrieved` / `reasoning` / `explanation` / `metadata.reason` field that a downstream model could read to know *why* a memory ranked?

Each step's outcome is recorded in the provider's `findings.yaml`. No subjective judgement — observable behaviour only.

## Findings schema

Each `providers/<name>/findings.yaml` is a flat structured record:

```yaml
provider: hindsight
version_under_test: "1.2.3"        # what we actually pip-installed / cloned
verified_at: 2026-05-13            # UTC date the harness was last run against this provider
verified_against: "Hermes 0.9.0 in LXC 129 / 192.168.4.x"

backend:                            # observable, not declared
  hosting: cloud | self-hosted | embedded
  requires_account: true | false
  requires_separate_server: true | false
  pip_footprint_mb: 12              # measured via `du -sm`

contract:
  initialize_ok: true | false
  remember_ok: true | false
  recall_ok: true | false
  recall_returned_results: 3        # actual count for the canonical query
  notes: ""

response_shape:
  why_retrieved_field: true | false        # is there a top-level field on each result that names *why* it ranked?
  why_retrieved_field_name: "reasoning"    # the actual key in their response (empty when false)
  per_result_score: true | false
  per_result_metadata: true | false

maintenance:
  duplicate_canonicalized: true | false | unknown   # did writing the same fact twice merge or duplicate?
  contradiction_surfaced: true | false | unknown    # does writing a conflicting fact produce a conflict record?
  contradiction_api: ""                              # tool name if surfaced; empty otherwise

skills:
  skill_tools_in_schema: true | false
  skill_tool_names: []                                # the actual *_skill_* tool names exposed

evidence:
  transcript_file: providers/hindsight/transcript.md  # human-readable session log
  raw_responses: providers/hindsight/raw/             # captured JSON of every response

couldnt_verify:
  reason: ""                                          # only populated when we explicitly skipped (e.g., cloud account unavailable)
  what_we_know_anyway: ""                             # plugin.yaml description + repo URL
```

When a step can't be exercised (e.g., a cloud provider requires an account we don't have), `couldnt_verify.reason` is populated explicitly and the relevant `contract.*` / `response_shape.*` / `maintenance.*` fields are set to `unknown` (not `false`). Honesty over coverage.

## Running the harness

```bash
# Against a single provider (smoke):
python -m tests.comparison.harness --provider holographic

# Against all of them:
python -m tests.comparison.harness --all

# Regenerate the markdown comparison table from findings:
python -m tests.comparison.compare > /tmp/comparison.md
```

## Environment

The harness runs against **LXC 129 (yantrik-memory-test)** which already has Hermes 0.9.0 + yantrikdb 0.7.6 installed (per `VERIFICATION.md`). Each provider is installed into a fresh Python venv to avoid cross-provider dep contamination, and each provider's findings are captured before tearing it down and moving to the next.

The harness can also run locally without LXC by stubbing Hermes (the same stub conftest the unit tests use). Local runs are useful for development; the LXC run is the one whose findings get published.

## Providers covered (planned)

| Provider | Status | Notes |
|---|---|---|
| yantrikdb (this) | TODO | Will use the existing embedded backend; baseline for the others. |
| hindsight | TODO | Self-hosted server; need to start its dependency. |
| holographic | TODO | Embedded SQLite — simplest to verify first. |
| honcho | TODO | Self-hosted server; honcho-server install required. |
| mem0 | TODO | Has both cloud + self-host modes; test self-host. |
| openviking | TODO | Local context DB; verify install path. |
| byterover | TODO (likely "couldn't verify") | Cloud-only; requires brv CLI auth. |
| retaindb | TODO (likely "couldn't verify") | Cloud-only API; requires account. |
| supermemory | TODO (likely "couldn't verify") | Cloud-only; requires account. |

## What this harness explicitly is NOT

- **Not a quality benchmark.** R@k / NDCG / latency numbers belong in a separate evaluation; this harness answers "what behaviours does the provider expose" not "how well does it expose them".
- **Not a value judgement.** Different providers pick different design points. A `false` cell means the provider doesn't expose that surface — not that the provider is worse.
- **Not exhaustive.** The seven probes above were picked because they correspond to behaviours users on Reddit asked about. Other behaviours (graph entity recall, summarization, namespace scoping, etc.) can be added as separate steps if they become relevant.
