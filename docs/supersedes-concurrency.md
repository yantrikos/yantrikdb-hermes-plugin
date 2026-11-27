# Concurrent skill revisions: what is atomic, and what is not

Scope: `EmbeddedYantrikDBClient.skill_define(..., supersedes_skill_id=…)`, i.e. the
write path that turns "v2 replaces v1" into a native `supersedes` link. This note
records what the code guarantees, where it stops guaranteeing it, and what the
remaining failure leaves behind. Numbers are counts from
`benchmarks/supersedes_race.py`, not estimates.

Reproduce everything below:

```
python benchmarks/supersedes_race.py --runs 40
```

## The invariant and where it lives

The engine keeps at most one *selected, active* inbound `supersedes` edge per
predecessor. That is the single-successor invariant, and it is enforced in
`link()`, in `gate_supersedes()` (`crates/yantrikdb-core/src/engine/links.rs`):
a `SELECT` over `record_links`, then the insert, both while the caller holds
`self.conn.lock()` — one `Mutex<Connection>` per engine instance.

The invariant is therefore **per engine handle**, not per database file. The
schema does not back it up: `record_links` has
`UNIQUE(source_rid, target_rid, link_type)`, which stops the same edge twice, but
nothing stops two *different* sources from both being `selected`/`active`
successors of one target. Two handles are two connections with two mutexes; the
`SELECT` of each can run before either `INSERT`, and then both inserts are
legal.

## Measured

One engine, two clients, two threads that both read v1 before either writes.
Measured on engine `0.23.6+vela.3d52394.2` (`_yantrikdb_rust.abi3.so`, sha256
`10e3ff62…`), which is byte-identical to the build the gateway has loaded:

```
40/40  one winner + typed refusal
       post-state: 1 edge on v1, 1 current version, 2 history rows
```

An engine-level probe (no plugin code, `link()` called concurrently on one
handle) agrees: 60/60 without a barrier and 60/60 with one, always exactly one
accepted edge.

Two engine handles on one file (`share_engine=False`), same two threads.
These are **samples**, not constants — the split varies run to run, so treat
them as a rate estimate with the run context attached, not as a fixed share:

```
sample A (this branch):   28/40 one winner + typed refusal, 12/40 two winners
sample B (independent):   22/40 one winner + typed refusal, 18/40 two winners
sample C (independent):   27/40 one winner + typed refusal, 13/40 two winners
```

Sample A was produced by `benchmarks/supersedes_race.py --runs 40` on engine
`0.23.6+vela.3d52394.2`; B and C by the independent reviewer with the same
script and the same engine build. What is stable across all three is the
*shape*: a substantial minority of runs accepts two successors, and in every
two-winner run exactly one writer carries `concurrent_revisions`. In all 12
two-winner runs of sample A both writers returned `stored=True`; afterwards the
predecessor had two inbound `supersedes` edges (measured via
`linked_records(v1, direction="inbound")`).

## The fork report is asymmetric

The provider cannot prevent the second write, so it repairs after the fact: a
writer that detects another successor of the same predecessor links its own new
record to that one (`_db.link(rid, other, "supersedes")`) and reports
`concurrent_revisions` + a `warning`. The repair converges — one of the two
records ends up superseded — but only one writer learns about it.

In 12 of 12 two-winner runs exactly one writer carried `concurrent_revisions`;
the other returned a plain success and its own engine still showed its record as
`is_current`. A caller that believes it wrote the current version is the state
to avoid, and it is not currently avoidable across handles.

## Crash semantics of the write

`skill_define` writes in two steps, because the `rid` is needed for the edge:

1. `record_text(...)` — the row is committed;
2. `link(rid, predecessor, "supersedes")` — the engine may refuse here.

The refusal path is covered: the row is removed with `forget()` and the caller
gets `YantrikDBClientError as "revised this skill concurrently"`, so a refusal
leaves neither an orphan row nor a second active version. Two windows remain,
and neither is reachable from Python:

- **Crash between (1) and (2).** SIGKILL, power loss or an interpreter abort
  between the two calls leaves a committed record with no `supersedes` edge. It
  is an active, unlinked duplicate that the fork detection cannot see, because
  fork detection looks for edges — and it is not merely invisible: measured on a
  real store, `_skill_current()` *selects the orphan as the current version*
  (two `current_status=active` rows, `current_is_orphan: true`), so the next
  reader is served the version nobody meant to keep. The repair is
  `skill_reify_revisions` (measured: `links_created: 1`, then one current
  version) or an explicit retirement; nothing detects the orphan on its own —
  the engine's own reify is explicit by design and runs neither at open nor in
  consolidation.
- **`record_with_links` on wheels without `link()`.** That path persists the
  record *before* validating the link, so a refusal leaves the row behind. The
  provider maps the resulting `RuntimeError` to a typed error but cannot undo the
  write; the code only takes that path when `_db.link` is missing.

## Operating envelope

A single engine handle per database file per process is the supported
configuration for skill revisions, and `share_engine=True` (the default) is what
provides it: the cache key is the absolute db path plus the embedder settings,
so every client in the process gets the same handle and therefore the same
connection mutex.

Opening a second handle on the same file — `share_engine=False`, or a second
process — is outside that envelope for concurrent revision writes. It is not
merely a lost race: an already-open handle also does not see records another
handle writes afterwards. In the two-winner reproduction the seed handle
returned only the predecessor from `skill_search` while `linked_records` showed
both new edges; a handle opened afterwards on a quiescent store returned all
rows (2/2, no concurrency). We did not investigate that further — it is recorded
because it decides what "two active versions served by search" can even mean
across handles: a single handle sees one writer at most.

## What the CI pins, and what it does not

`tests/test_provider.py::TestRealConcurrentRevision` asserts the in-engine case:
two clients, one engine, exactly one success, one typed refusal, one current
version. It asserts the engine identity as a premise, so a future change to the
cache cannot silently turn it into a cross-handle test.

The cross-handle case is a reproducible engine finding, not a CI assertion: the
outcome varies between runs (samples above), so a green/red test would encode a
coin flip. `benchmarks/supersedes_race.py` stays in the tree as the reproducer.
This is a deliberate scope decision: the provider is supported with one handle
per file, and closing the cross-handle case at the provider level would mean
building a second, process-spanning lock instead of fixing the invariant where
it belongs.

The engine fix is a partial unique index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS record_links_single_successor
    ON record_links(target_rid, link_type)
    WHERE link_type = 'supersedes'
      AND selection_state = 'selected'
      AND status = 'active';
```

That makes the invariant a property of the database instead of a property of
"everyone holds the same mutex", which also covers a second process and a second
handle. Candidate edges stay possible — replication already inserts supersedes
candidates as `rejected_conflict`, retraction writes `retracted`, and the
canonical fold demotes all candidates before promoting one — so the index
matches the design intent rather than narrowing it. Probed on a clean schema: the
first selected edge is accepted, the second is refused with
`UNIQUE constraint failed: record_links.target_rid, record_links.link_type`, and
`rejected_conflict` / `retracted` rows still insert.

**It is not a drop-in change**, for two reasons:

- Existing stores that are already in the two-winner state cannot create the
  index: `CREATE UNIQUE INDEX` aborts on the duplicate rows. Because the schema
  is applied as one batch at engine open, that turns the index into a store that
  no longer opens. It needs an idempotent demotion/refold migration *before*
  index creation — the same canonical fold that already resolves candidates,
  with a deterministic winner (the plugin's repair uses "later write wins";
  the migration should use the same rule so both agree).
- A losing writer would get a raw SQLite `UNIQUE` error instead of
  `YantrikDbError::SupersedeConflict`. The gate should stay the primary path (it
  returns the typed error) with the index as the backstop, or `link()` must map
  the constraint violation to `SupersedeConflict` so every engine consumer sees
  the same error class.
